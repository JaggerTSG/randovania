import copy
import json
import threading
import time
from pathlib import Path
from typing import Tuple, List, Optional, Dict

import dolphin_memory_engine
import pika
import slugify
from PySide2.QtCore import QTimer, Signal, Qt
from PySide2.QtGui import QPixmap
from PySide2.QtWidgets import QMainWindow, QLabel, QListWidgetItem
from pika.adapters.blocking_connection import BlockingChannel

from randovania import get_data_path
from randovania.game_description import data_reader
from randovania.game_description.resources.pickup_entry import PickupEntry
from randovania.game_description.resources.pickup_index import PickupIndex
from randovania.game_description.resources.resource_database import find_resource_info_with_long_name
from randovania.game_description.resources.resource_type import ResourceType
from randovania.game_description.resources.simple_resource_info import SimpleResourceInfo
from randovania.games.prime import default_data
from randovania.gui.generated.dolphin_hook_window_ui import Ui_DolphinHookWindow
from randovania.gui.lib import common_qt_lib
from randovania.gui.lib.clickable_label import ClickableLabel
from randovania.gui.lib.pixmap_lib import paint_with_opacity
from randovania.interface_common.players_configuration import PlayersConfiguration
from randovania.layout.layout_description import LayoutDescription


class ServerConnection(threading.Thread):
    _active: bool = True

    def __init__(self, channel: BlockingChannel):
        super().__init__()
        self._channel = channel

    def run(self) -> None:
        while self._active:
            try:
                self._channel.start_consuming()
            except Exception as e:
                print(e)
                time.sleep(1)

    def quit(self):
        self._active = False
        try:
            self._channel.stop_consuming()
        except AssertionError:
            pass


class DolphinHookWindow(QMainWindow, Ui_DolphinHookWindow):
    _hooked = False
    _base_address: int
    give_item_signal = Signal(PickupEntry)

    def __init__(self, layout: LayoutDescription, players_config: PlayersConfiguration):
        super().__init__()
        self.setupUi(self)
        common_qt_lib.set_default_window_icon(self)

        self.layout = layout
        if players_config is not None:
            self.player_index = players_config.player_index
            self.player_names = players_config.player_names

        # self.game_data = data_reader.decode_data(layout.permalink.layout_configuration.game_data)
        self.game_data = data_reader.decode_data(default_data.decode_default_prime2())
        self._placeholder_item = self.game_data.resource_database.get_by_type_and_index(ResourceType.ITEM, 74)
        self._energy_tank_item = find_resource_info_with_long_name(self.game_data.resource_database.item, "Energy Tank")
        self.channel = None

        self._pending_receive: List[PickupEntry] = []
        self._pending_send: List[int] = []

        self._item_to_label: Dict[SimpleResourceInfo, ClickableLabel] = {}
        self._labels_for_keys = []
        self.create_tracker()

        self._update_timer = QTimer(self)
        self._update_timer.setInterval(100)

        self.give_item_signal.connect(self.give_pickup)
        self.connect_button.clicked.connect(self.connect_to_server)
        self._update_timer.timeout.connect(self._on_timer_update)
        if layout is None:
            self.player_name_label.setText(f"No game provided, acting as tracker only.")
        else:
            self.player_name_label.setText(self.player_names[self.player_index])

        self.connect_button.setEnabled(layout is not None)
        self._update_timer.start()

    def connect_to_server(self):
        parameters = pika.URLParameters('amqp://randovania:multiworld@uspgamedev.org:5672/%2F')
        # parameters = pika.URLParameters('amqp://randovania:multiworld@192.168.0.59:5672/%2F')
        self.connection = pika.BlockingConnection(parameters)

        self.channel = self.connection.channel()

        seed_hash = self.layout.shareable_hash.lower()
        for i in range(self.layout.permalink.player_count):
            self.channel.queue_declare(queue=f"multiworld-{seed_hash}-{i}", durable=True,
                                       exclusive=False, auto_delete=False)

        self.channel.basic_consume(queue=f"multiworld-{seed_hash}-{self.player_index}",
                                   on_message_callback=self._on_channel_callback,
                                   auto_ack=False)

        self.thread = ServerConnection(self.channel)
        self.thread.start()
        self.connect_button.setText("Connected")
        self.connect_button.setEnabled(False)

    def _on_channel_callback(self, ch: BlockingChannel, method, properties, body):
        item_event = json.loads(body)
        source_player = item_event["source"]

        target = self.layout.all_patches[source_player].pickup_assignment.get(PickupIndex(item_event["target"]))
        if target is not None:
            if target.player != self.player_index:
                print(">>> WARNING! received pickup wasn't for us")

            self.add_log_entry(f"Received {target.pickup.name} from {self.player_names[source_player]}")
            self.give_item_signal.emit(target.pickup)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def _addresses_for_item(self, item: SimpleResourceInfo) -> Tuple[int, int]:
        multi = self._base_address + item.index * 12
        return 92 + multi, 96 + multi

    def _hook_to_dolphin(self):
        dolphin_memory_engine.hook()

        if not dolphin_memory_engine.is_hooked():
            raise RuntimeError("Unable to connect to Dolphin. Is it open, with the game running?")

        try:
            if dolphin_memory_engine.read_word(0x80000000) != 1194478917:
                raise RuntimeError("Dolphin is not running Metroid Prime 2: Echoes")

            self._ingame_timer_address = dolphin_memory_engine.follow_pointers(0x803C5C9C, [0x48])
            if dolphin_memory_engine.read_double(self._ingame_timer_address) < 0.5:
                raise RuntimeError("Please load a save file")

            self._base_address = dolphin_memory_engine.follow_pointers(0x803CA740, [4884, 0])
            self._placeholder_addresses = self._addresses_for_item(self._placeholder_item)
            self._hooked = True

        except RuntimeError:
            dolphin_memory_engine.un_hook()
            raise

    def _unhook_from_dolphin(self):
        dolphin_memory_engine.un_hook()
        self._hooked = False
        self.hook_status_label.setText("Disconnected from Dolphin")

    def _update_tracker_from_hook(self):
        for item, label in self._item_to_label.items():
            address_current, address_capacity = self._addresses_for_item(item)
            current = dolphin_memory_engine.read_word(address_current)
            label.set_checked(current > 0)

        address_current, address_capacity = self._addresses_for_item(self._energy_tank_item)
        self._energy_tank_label.setText("x {}/14".format(dolphin_memory_engine.read_word(address_current)))

        for label, keys in self._labels_for_keys:
            num_keys = 0
            for key in keys:
                address_current, address_capacity = self._addresses_for_item(key)
                num_keys += dolphin_memory_engine.read_word(address_current)
            label.setText("x {}/{}".format(num_keys, len(keys)))

    def add_log_entry(self, log: str):
        QListWidgetItem(self.history_list).setText(log)

    def give_pickup(self, entry: PickupEntry):
        if not self._hooked:
            self._pending_receive.append(entry)
            return

        current_resources = {}
        for item in self.game_data.resource_database.item:
            current_resources[item] = dolphin_memory_engine.read_word(self._addresses_for_item(item)[0])

        for item, delta in entry.resource_gain(copy.copy(current_resources)):
            if item.index == 47:
                # Item% was already set
                continue

            if delta == 0:
                continue

            address_current, address_capacity = self._addresses_for_item(item)
            current = dolphin_memory_engine.read_word(address_current)
            capacity = dolphin_memory_engine.read_word(address_capacity)

            current += delta
            capacity += delta

            dolphin_memory_engine.write_word(address_current, current)
            dolphin_memory_engine.write_word(address_capacity, capacity)
            self.add_log_entry(f"Set {item.long_name} to {current}/{capacity}")

            if item.index == 13:  # Dark Suit
                dolphin_memory_engine.write_word(self._base_address + 84, 1)
            elif item.index == 14:  # Light Suit
                dolphin_memory_engine.write_word(self._base_address + 84, 2)
            elif item.index == self._energy_tank_item.index:
                dolphin_memory_engine.write_float(self._base_address + 20, capacity * 100 + 99)

    def _on_timer_update(self):
        if not dolphin_memory_engine.is_hooked() or not self._hooked:
            try:
                self._hook_to_dolphin()
                self.hook_status_label.setText("Connected to Dolphin")
            except RuntimeError as e:
                self.hook_status_label.setText(str(e))
                return

        try:
            if dolphin_memory_engine.read_double(self._ingame_timer_address) < 0.5:
                raise RuntimeError("Please load a save file")
            collected_pickup = dolphin_memory_engine.read_word(self._placeholder_addresses[0])
        except RuntimeError:
            self._unhook_from_dolphin()
            return

        while self._pending_receive:
            self.give_pickup(self._pending_receive.pop(0))

        while self.channel and self._pending_send:
            self.send_pickup_target(self._pending_send.pop(0))

        self._update_tracker_from_hook()
        if collected_pickup == 0 or self.layout is None:
            return

        dolphin_memory_engine.write_word(self._placeholder_addresses[0], 0)
        dolphin_memory_engine.write_word(self._placeholder_addresses[1], 0)

        collected_index = collected_pickup - 1

        pickup_target = self.layout.all_patches[self.player_index].pickup_assignment.get(PickupIndex(collected_index))
        if pickup_target is not None:
            if pickup_target.player == self.player_index:
                self.give_item_signal.emit(pickup_target.pickup)
            else:
                self._pending_send.append(collected_index)

    def send_pickup_target(self, collected_index: int):
        pickup_target = self.layout.all_patches[self.player_index].pickup_assignment.get(PickupIndex(collected_index))

        self.add_log_entry(f"Sending {pickup_target.pickup.name} to {self.player_names[pickup_target.player]}")

        seed_hash = self.layout.shareable_hash.lower()
        self.channel.basic_publish(exchange='',
                                   routing_key=f'multiworld-{seed_hash}-{pickup_target.player}',
                                   body=json.dumps({"source": self.player_index, "target": collected_index}),
                                   properties=pika.BasicProperties(content_type='text/plain',
                                                                   delivery_mode=2))

    def create_tracker(self):
        def get_image_path(image_name: str) -> Path:
            return get_data_path().joinpath(f"gui_assets/tracker/images-noanim/{image_name}.gif")

        def find_resource(name: str):
            return find_resource_info_with_long_name(self.game_data.resource_database.item, name)

        def create_item(image_name: str, always_opaque: bool = False):
            image_path = get_image_path(image_name)
            pixmap = QPixmap(str(image_path))

            label = ClickableLabel(self.inventory_group, paint_with_opacity(pixmap, 0.3),
                                   paint_with_opacity(pixmap, 1.0))
            label.set_checked(always_opaque)
            label.set_ignore_mouse_events(always_opaque)

            return label

        def add_item(row: int, column: int, item_name: str, image_name: Optional[str] = None):
            if image_name is None:
                image_name = slugify.slugify(item_name).replace("-", "_")

            label = create_item(image_name)
            label.set_ignore_mouse_events(True)
            self.inventory_layout.addWidget(label, row, column)

            self._item_to_label[find_resource(item_name)] = label

        add_item(0, 0, "Missile Launcher")
        add_item(1, 0, "Super Missile")
        add_item(2, 0, "Seeker Launcher")
        add_item(3, 0, "Energy Transfer Module")

        add_item(0, 1, "Dark Beam")
        add_item(0, 2, "Light Beam")
        add_item(0, 3, "Annihilator Beam")
        add_item(1, 1, "Darkburst")
        add_item(1, 2, "Sunburst")
        add_item(1, 3, "Sonic Boom")

        add_item(2, 2, "Dark Visor")
        add_item(2, 3, "Echo Visor")

        add_item(3, 1, "Space Jump Boots")
        add_item(3, 2, "Gravity Boost")
        add_item(3, 3, "Grapple Beam")
        add_item(3, 4, "Screw Attack")

        add_item(0, 4, "Dark Suit")
        add_item(1, 4, "Light Suit")

        add_item(0, 5, "Morph Ball Bomb")
        add_item(1, 5, "Power Bomb")
        add_item(2, 5, "Boost Ball")
        add_item(3, 5, "Spider Ball")

        add_item(0, 6, "Violet Translator")
        add_item(1, 6, "Amber Translator")
        add_item(2, 6, "Emerald Translator")
        add_item(3, 6, "Cobalt Translator")

        self.inventory_layout.addWidget(create_item("energy_tank", True), 5, 0)
        energy_tank_label = QLabel(self.inventory_group)
        energy_tank_label.setText("x 0/14")
        energy_tank_label.setAlignment(Qt.AlignCenter)
        self.inventory_layout.addWidget(energy_tank_label, 6, 0)
        self._energy_tank_label = energy_tank_label

        self.inventory_layout.addWidget(create_item("dark_agon_key-recolored", True), 5, 1)
        dark_agon_key_label = QLabel(self.inventory_group)
        dark_agon_key_label.setText("x 0/3")
        dark_agon_key_label.setAlignment(Qt.AlignCenter)
        self.inventory_layout.addWidget(dark_agon_key_label, 6, 1)
        self._labels_for_keys.append((
            dark_agon_key_label,
            (find_resource("Dark Agon Key 1"), find_resource("Dark Agon Key 2"), find_resource("Dark Agon Key 3"))
        ))

        self.inventory_layout.addWidget(create_item("dark_torvus_key-recolored", True), 5, 2)
        dark_torvus_key_label = QLabel(self.inventory_group)
        dark_torvus_key_label.setText("x 0/3")
        dark_torvus_key_label.setAlignment(Qt.AlignCenter)
        self.inventory_layout.addWidget(dark_torvus_key_label, 6, 2)
        self._labels_for_keys.append((
            dark_torvus_key_label,
            (find_resource("Dark Torvus Key 1"), find_resource("Dark Torvus Key 2"), find_resource("Dark Torvus Key 3"))
        ))

        self.inventory_layout.addWidget(create_item("ing_hive_key-recolored", True), 5, 3)
        ing_hive_key_label = QLabel(self.inventory_group)
        ing_hive_key_label.setText("x 0/3")
        ing_hive_key_label.setAlignment(Qt.AlignCenter)
        self.inventory_layout.addWidget(ing_hive_key_label, 6, 3)
        self._labels_for_keys.append((
            ing_hive_key_label,
            (find_resource("Ing Hive Key 1"), find_resource("Ing Hive Key 2"), find_resource("Ing Hive Key 3"))
        ))

        self.inventory_layout.addWidget(create_item("sky_temple_key", True), 5, 4)
        sky_temple_key_label = QLabel(self.inventory_group)
        sky_temple_key_label.setText("x 0/9")
        sky_temple_key_label.setAlignment(Qt.AlignCenter)
        self.inventory_layout.addWidget(sky_temple_key_label, 6, 4)
        self._labels_for_keys.append((
            sky_temple_key_label,
            (find_resource("Sky Temple Key 1"), find_resource("Sky Temple Key 2"), find_resource("Sky Temple Key 3"),
             find_resource("Sky Temple Key 4"), find_resource("Sky Temple Key 5"), find_resource("Sky Temple Key 6"),
             find_resource("Sky Temple Key 7"), find_resource("Sky Temple Key 8"), find_resource("Sky Temple Key 9"),)
        ))

    def closeEvent(self, event):
        if self.channel is not None:
            self.thread.quit()
        super().closeEvent(event)
