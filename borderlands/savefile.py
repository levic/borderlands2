#! /usr/bin/env python
import binascii
from bisect import insort
from cStringIO import StringIO
import hashlib
import json
import math
import argparse
import random
import struct
import sys
import os

class AssetLibrary(object):

    def __init__(self):
        filename = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            'AssetLibrary.json'
        )
        with open(filename, 'r') as fp:
            data = json.load(fp)

        def extract_asset_lib(lib_name):
            return {
                'assets': {
                    set_data['id']: set_data['libraries'][lib_name]['sublibraries']
                    for set_data in data['sets']
                },
                'ignore': [
                    (
                        (1 << data['configs'][lib_name]['sublibrary_bits']) - 1,
                        (1 << data['configs'][lib_name]['asset_bits']) - 1,
                    ),
                ],
            }

        self.manufacturers = extract_asset_lib('Manufacturers')
        self.balance_defs = extract_asset_lib('BalanceDefs')
        self.weapon_types = extract_asset_lib('WeaponTypes')
        self.weapon_parts = extract_asset_lib('WeaponParts')
        self.item_types = extract_asset_lib('ItemTypes')
        self.item_parts = extract_asset_lib('ItemParts')

    def _lookup(self, data, set_id, asset_lib):
        if asset_lib is None:
            return ''
        if (asset_lib['lib'], asset_lib['asset']) in data['ignore']:
            return ''

        try:
            x = data['assets'][set_id][asset_lib['lib']]['assets'][asset_lib['asset']]
            # enable the following for short asset names
            x = '.'.join(x.split('.')[-2:])
            return x
        except IndexError:
            return '%(lib)d.%(asset)d' % asset_lib

    def get_manufacturer(self, set_id, asset_lib):
        return self._lookup(self.manufacturers, set_id, asset_lib)

    def get_balance_type(self, set_id, asset_lib):
        return self._lookup(self.balance_defs, set_id, asset_lib)

    def get_weapon_type(self, set_id, asset_lib):
        return self._lookup(self.weapon_types, set_id, asset_lib)

    def get_weapon_part(self, set_id, asset_lib):
        return self._lookup(self.weapon_parts, set_id, asset_lib)

    def get_item_type(self, set_id, asset_lib):
        return self._lookup(self.item_types, set_id, asset_lib)

    def get_item_part(self, set_id, asset_lib):
        return self._lookup(self.item_parts, set_id, asset_lib)

class Config(argparse.Namespace):
    """
    Class to hold our configuration information.  Note that
    we're NOT using a separate class for BL2 and BLTPS configs,
    since so much of it is the same.
    """

    # Given by the user, booleans
    json = False
    bigendian = False
    verbose = True
    force = False

    # Given by the user, strings
    import_items = None
    output = 'savegame'
    input_filename = '-'
    output_filename = '-'

    # Former 'modify' options
    name = None
    save_game_id = None
    level = None
    money = None
    eridium = None
    moonstone = None
    seraph = None
    torgue = None
    itemlevels = None
    backpack = None
    bank = None
    gunslots = None
    maxammo = None
    unlock = {}
    challenges = {}
    
    # Config options interpreted from the above
    endian = '<'
    changes = False

    def finish(self, parser, app):
        """
        Some extra sanity checks on our options.  "parser" should
        be an active ArgumentParser object we can use to raise
        errors.  "app" is an App object which we use for a couple
        lookups.
        """

        # Endianness
        if self.bigendian:
            self.endian = '>' 
        else:
            self.endian = '<'

        # If we're unlocking ammo, also set maxammo
        if 'ammo' in self.unlock:
            self.maxammo = True

        # Set our "changes" boolean
        if any([var is not None for var in [self.name,
                self.save_game_id, self.level,
                self.money, self.eridium, self.moonstone,
                self.seraph, self.seraph, self.torgue,
                self.itemlevels, self.backpack, self.bank,
                self.gunslots, self.maxammo]]):
            self.changes = True
        if any([len(var) > 0 for var in [self.unlock, self.challenges]]):
            self.changes = True

        # Can't read/write to the same file
        if self.input_filename == self.output_filename and self.input_filename != '-':
            parser.error('input_filename and output_filename cannot be the same file')

        # Sort out 'backpack'
        if self.backpack is not None:
            if self.backpack == 'max':
                self.backpack = app.max_backpack_size
            else:
                try:
                    self.backpack = int(self.backpack)
                except ValueError:
                    parser.error('Backpack value "%s" is not a number' % (self.backpack))
                if self.backpack > app.max_backpack_size:
                    self.backpack = app.max_backpack_size
                elif self.backpack < app.min_backpack_size:
                    self.backpack = app.min_backpack_size

        # Sort out bank
        if self.bank is not None:
            if self.bank == 'max':
                self.bank = app.max_bank_size
            else:
                try:
                    self.bank = int(self.bank)
                except ValueError:
                    parser.error('Backpack value "%s" is not a number' % (self.bank))
                if self.bank > app.max_bank_size:
                    self.bank = app.max_bank_size
                elif self.bank < app.min_bank_size:
                    self.bank = app.min_bank_size

class DictAction(argparse.Action):
    """
    Custom argparse action to put list-like arguments into
    a dict (where the value will be True) rather than a list.
    This is probably implemented fairly shoddily.
    """
    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        """
        Constructor, taken right from https://docs.python.org/2.7/library/argparse.html#action
        """
        if nargs is not None:
            raise ValueError('nargs is not allowed')
        super(DictAction, self).__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        """
        Actually setting a value.  Forces the attr into a dict if it isn't already.
        """
        arg_value = getattr(namespace, self.dest)
        if not isinstance(arg_value, dict):
            arg_value = {}
        arg_value[values] = True
        setattr(namespace, self.dest, arg_value)

class BorderlandsError(Exception): pass


class ReadBitstream(object):

    def __init__(self, s):
        self.s = s
        self.i = 0

    def read_bit(self):
        i = self.i
        self.i = i + 1
        byte = ord(self.s[i >> 3])
        bit = byte >> (7 - (i & 7))
        return bit & 1

    def read_bits(self, n):
        s = self.s
        i = self.i
        end = i + n
        chunk = s[i >> 3: (end + 7) >> 3]
        value = ord(chunk[0]) &~ (0xff00 >> (i & 7))
        for c in chunk[1: ]:
            value = (value << 8) | ord(c)
        if (end & 7) != 0:
            value = value >> (8 - (end & 7))
        self.i = end
        return value

    def read_byte(self):
        i = self.i
        self.i = i + 8
        byte = ord(self.s[i >> 3])
        if (i & 7) == 0:
            return byte
        byte = (byte << 8) | ord(self.s[(i >> 3) + 1])
        return (byte >> (8 - (i & 7))) & 0xff

class WriteBitstream(object):

    def __init__(self):
        self.s = ""
        self.byte = 0
        self.i = 7

    def write_bit(self, b):
        i = self.i
        byte = self.byte | (b << i)
        if i == 0:
            self.s += chr(byte)
            self.byte = 0
            self.i = 7
        else:
            self.byte = byte
            self.i = i - 1

    def write_bits(self, b, n):
        s = self.s
        byte = self.byte
        i = self.i
        while n >= (i + 1):
            shift = n - (i + 1)
            n = n - (i + 1)
            byte = byte | (b >> shift)
            b = b &~ (byte << shift)
            s = s + chr(byte)
            byte = 0
            i = 7
        if n > 0:
            byte = byte | (b << (i + 1 - n))
            i = i - n
        self.s = s
        self.byte = byte
        self.i = i

    def write_byte(self, b):
        i = self.i
        if i == 7:
            self.s += chr(b)
        else:
            self.s += chr(self.byte | (b >> (7 - i)))
            self.byte = (b << (i + 1)) & 0xff

    def getvalue(self):
        if self.i != 7:
            return self.s + chr(self.byte)
        else:
            return self.s

class ChallengeCat(object):
    """
    Simple little class to hold information about challenge
    categories.  Mostly just a glorified dict.
    """

    def __init__(self, name, dlc=0):
        self.name = name
        self.dlc = dlc
        if self.dlc == 0:
            self.is_from_dlc = 0
        else:
            self.is_from_dlc = 1

class Challenge(object):
    """
    A simple little object to hold information about our non-level-specific
    challenges.  This is *mostly* just a glorified dict.
    """

    def __init__(self, position, identifier, id_text, cat, name, description, levels, bonus=None):
        self.position = position
        self.identifier = identifier
        self.id_text = id_text
        self.cat = cat
        self.name = name
        self.description = description
        self.levels = levels
        self.bonus = bonus

    def get_max(self):
        """
        Returns the point value for the challenge JUST before its maximum level.
        """
        return self.levels[-1] - 1

    def get_bonus(self):
        """
        Returns the point value for the challenge JUST before getting the challenge's
        bonus reward, if any.  Will return None if no bonus is present for the
        challenge.
        """
        if self.bonus:
            return self.levels[self.bonus-1] - 1
        else:
            return None

class App(object):
    """
    Our main application class.
    """

    # These seem to be the same for both BL2 and BLTPS
    item_sizes = (
        (8, 17, 20, 11, 7, 7, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16),
        (8, 13, 20, 11, 7, 7, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17)
    )

    # Ditto
    item_header_sizes = (
        (("type", 8), ("balance", 10), ("manufacturer", 7)),
        (("type", 6), ("balance", 10), ("manufacturer", 7))
    )

    # Ditto
    clz_table = (
        32, 0, 1, 26, 2, 23, 27, 0, 3, 16, 24, 30, 28, 11, 0, 13, 4,
        7, 17, 0, 25, 22, 31, 15, 29, 10, 12, 6, 0, 21, 14, 9, 5,
        20, 8, 19, 18
    )

    min_backpack_size=12
    max_backpack_size=39
    min_bank_size=6
    max_bank_size=24

    # "laser" in here doesn't apply to B2, but it won't hurt anything
    # because we process ammo pools based off the black market values,
    # which won't include lasers for B2
    ammo_resources = {
        'rifle': ('D_Resources.AmmoResources.Ammo_Combat_Rifle', 'D_Resourcepools.AmmoPools.Ammo_Combat_Rifle_Pool'),
        'shotgun': ('D_Resources.AmmoResources.Ammo_Combat_Shotgun', 'D_Resourcepools.AmmoPools.Ammo_Combat_Shotgun_Pool'),
        'grenade': ('D_Resources.AmmoResources.Ammo_Grenade_Protean', 'D_Resourcepools.AmmoPools.Ammo_Grenade_Protean_Pool'),
        'smg': ('D_Resources.AmmoResources.Ammo_Patrol_SMG', 'D_Resourcepools.AmmoPools.Ammo_Patrol_SMG_Pool'),
        'pistol': ('D_Resources.AmmoResources.Ammo_Repeater_Pistol', 'D_Resourcepools.AmmoPools.Ammo_Repeater_Pistol_Pool'),
        'launcher': ('D_Resources.AmmoResources.Ammo_Rocket_Launcher', 'D_Resourcepools.AmmoPools.Ammo_Rocket_Launcher_Pool'),
        'sniper': ('D_Resources.AmmoResources.Ammo_Sniper_Rifle', 'D_Resourcepools.AmmoPools.Ammo_Sniper_Rifle_Pool'),
        'laser': ('D_Resources.AmmoResources.Ammo_Combat_Laser', 'D_Resourcepools.AmmoPools.Ammo_Combat_Laser_Pool'),
    }

    def read_huffman_tree(self, b):
        node_type = b.read_bit()
        if node_type == 0:
            return (None, (self.read_huffman_tree(b), self.read_huffman_tree(b)))
        else:
            return (None, b.read_byte())

    def write_huffman_tree(self, node, b):
        if type(node[1]) is int:
            b.write_bit(1)
            b.write_byte(node[1])
        else:
            b.write_bit(0)
            self.write_huffman_tree(node[1][0], b)
            self.write_huffman_tree(node[1][1], b)

    def make_huffman_tree(self, data):
        frequencies = [0] * 256
        for c in data:
            frequencies[ord(c)] += 1

        nodes = [[f, i] for (i, f) in enumerate(frequencies) if f != 0]
        nodes.sort()

        while len(nodes) > 1:
            l, r = nodes[: 2]
            nodes = nodes[2: ]
            insort(nodes, [l[0] + r[0], [l, r]])

        return nodes[0]

    def invert_tree(self, node, code=0, bits=0):
        if type(node[1]) is int:
            return {chr(node[1]): (code, bits)}
        else:
            d = {}
            d.update(self.invert_tree(node[1][0], code << 1, bits + 1))
            d.update(self.invert_tree(node[1][1], (code << 1) | 1, bits + 1))
            return d

    def huffman_decompress(self, tree, bitstream, size):
        output = ""
        while len(output) < size:
            node = tree
            while 1:
                b = bitstream.read_bit()
                node = node[1][b]
                if type(node[1]) is int:
                    output += chr(node[1])
                    break
        return output

    def huffman_compress(self, encoding, data, bitstream):
        for c in data:
            code, nbits = encoding[c]
            bitstream.write_bits(code, nbits)


    def pack_item_values(self, is_weapon, values):
        i = 0
        bytes = [0] * 32
        for value, size in zip(values, self.item_sizes[is_weapon]):
            if value is None:
                break
            j = i >> 3
            value = value << (i & 7)
            while value != 0:
                bytes[j] |= value & 0xff
                value = value >> 8
                j = j + 1
            i = i + size
        if (i & 7) != 0:
            value = 0xff << (i & 7)
            bytes[i >> 3] |= (value & 0xff)
        return "".join(map(chr, bytes[: (i + 7) >> 3]))

    def unpack_item_values(self, is_weapon, data):
        i = 8
        data = " " + data
        values = []
        end = len(data) * 8
        for size in self.item_sizes[is_weapon]:
            j = i + size
            if j > end:
                values.append(None)
                continue
            value = 0
            for b in data[j >> 3: (i >> 3) - 1: -1]:
                value = (value << 8) | ord(b)
            values.append((value >> (i & 7)) &~ (0xff << size))
            i = j
        return values

    def rotate_data_right(self, data, steps):
        steps = steps % len(data)
        return data[-steps: ] + data[: -steps]

    def rotate_data_left(self, data, steps):
        steps = steps % len(data)
        return data[steps: ] + data[: steps]

    def xor_data(self, data, key):
        key = key & 0xffffffff
        output = ""
        for c in data:
            key = (key * 279470273) % 4294967291
            output += chr((ord(c) ^ key) & 0xff)
        return output

    def wrap_item(self, is_weapon, values, key):
        item = self.pack_item_values(is_weapon, values)
        header = struct.pack(">Bi", (is_weapon << 7) | self.item_struct_version, key)
        padding = "\xff" * (33 - len(item))
        h = binascii.crc32(header + "\xff\xff" + item + padding) & 0xffffffff
        checksum = struct.pack(">H", ((h >> 16) ^ h) & 0xffff)
        body = self.xor_data(self.rotate_data_left(checksum + item, key & 31), key >> 5)
        return header + body

    def unwrap_item(self, data):
        version_type, key = struct.unpack(">Bi", data[: 5])
        is_weapon = version_type >> 7
        raw = self.rotate_data_right(self.xor_data(data[5: ], key >> 5), key & 31)
        return is_weapon, self.unpack_item_values(is_weapon, raw[2: ]), key

    def replace_raw_item_key(self, data, key):
        old_key = struct.unpack(">i", data[1: 5])[0]
        item = self.rotate_data_right(self.xor_data(data[5: ], old_key >> 5), old_key & 31)[2: ]
        header = data[0] + struct.pack(">i", key)
        padding = "\xff" * (33 - len(item))
        h = binascii.crc32(header + "\xff\xff" + item + padding) & 0xffffffff
        checksum = struct.pack(">H", ((h >> 16) ^ h) & 0xffff)
        body = self.xor_data(self.rotate_data_left(checksum + item, key & 31), key >> 5)
        return header + body

    def read_varint(self, f):
        value = 0
        offset = 0
        while 1:
            b = ord(f.read(1))
            value |= (b & 0x7f) << offset
            if (b & 0x80) == 0:
                break
            offset = offset + 7
        return value

    def write_varint(self, f, i):
        while i > 0x7f:
            f.write(chr(0x80 | (i & 0x7f)))
            i = i >> 7
        f.write(chr(i))

    def read_protobuf(self, data):
        fields = {}
        end_position = len(data)
        bytestream = StringIO(data)
        while bytestream.tell() < end_position:
            key = self.read_varint(bytestream)
            field_number = key >> 3
            wire_type = key & 7
            value = self.read_protobuf_value(bytestream, wire_type)
            fields.setdefault(field_number, []).append([wire_type, value])
        return fields

    def read_protobuf_value(self, b, wire_type):
        if wire_type == 0:
            value = self.read_varint(b)
        elif wire_type == 1:
            value = struct.unpack("<Q", b.read(8))[0]
        elif wire_type == 2:
            length = self.read_varint(b)
            value = b.read(length)
        elif wire_type == 5:
            value = struct.unpack("<I", b.read(4))[0]
        else:
            raise BorderlandsError("Unsupported wire type " + str(wire_type))
        return value

    def read_repeated_protobuf_value(self, data, wire_type):
        b = StringIO(data)
        values = []
        while b.tell() < len(data):
            values.append(self.read_protobuf_value(b, wire_type))
        return values

    def write_protobuf(self, data):
        b = StringIO()
        # If the data came from a JSON file the keys will all be strings
        data = dict([(int(k), v) for (k, v) in data.items()])
        for key, entries in sorted(data.items()):
            for wire_type, value in entries:
                if type(value) is dict:
                    value = self.write_protobuf(value)
                    wire_type = 2
                elif type(value) in (list, tuple) and wire_type != 2:
                    sub_b = StringIO()
                    for v in value:
                        self.write_protobuf_value(sub_b, wire_type, v)
                    value = sub_b.getvalue()
                    wire_type = 2
                self.write_varint(b, (key << 3) | wire_type)
                self.write_protobuf_value(b, wire_type, value)
        return b.getvalue()

    def write_protobuf_value(self, b, wire_type, value):
        if wire_type == 0:
            self.write_varint(b, value)
        elif wire_type == 1:
            b.write(struct.pack("<Q", value))
        elif wire_type == 2:
            if type(value) is unicode:
                value = value.encode("latin1")
            elif type(value) is list:
                value = "".join(map(chr, value))
            self.write_varint(b, len(value))
            b.write(value)
        elif wire_type == 5:
            b.write(struct.pack("<I", value))
        else:
            raise BorderlandsError("Unsupported wire type " + str(wire_type))

    def write_repeated_protobuf_value(self, data, wire_type):
        b = StringIO()
        for value in data:
            self.write_protobuf_value(b, wire_type, value)
        return b.getvalue()

    def parse_zigzag(self, i):
        if i & 1:
            return -1 ^ (i >> 1)
        else:
            return i >> 1

    def apply_structure(self, pbdata, s):
        fields = {}
        raw = {}
        for k, data in pbdata.items():
            mapping = s.get(k)
            if mapping is None:
                raw[k] = data
                continue
            elif type(mapping) is str:
                fields[mapping] = data[0][1]
                continue
            key, repeated, child_s = mapping
            if child_s is None:
                values = [d[1] for d in data]
                fields[key] = values if repeated else values[0]
            elif type(child_s) is int:
                if repeated:
                    fields[key] = self.read_repeated_protobuf_value(data[0][1], child_s)
                else:
                    fields[key] = data[0][1]
            elif type(child_s) is tuple:
                values = [child_s[0](d[1]) for d in data]
                fields[key] = values if repeated else values[0]
            elif type(child_s) is dict:
                values = [self.apply_structure(self.read_protobuf(d[1]), child_s) for d in data]
                fields[key] = values if repeated else values[0]
            else:
                raise Exception("Invalid mapping %r for %r: %r" % (mapping, k, data))
        if len(raw) != 0:
            fields["_raw"] = {}
            for k, values in raw.items():
                safe_values = []
                for (wire_type, v) in values:
                    if wire_type == 2:
                        v = [ord(c) for c in v]
                    safe_values.append([wire_type, v])
                fields["_raw"][k] = safe_values
        return fields

    def remove_structure(self, data, inv):
        pbdata = {}
        pbdata.update(data.get("_raw", {}))
        for k, value in data.items():
            if k == "_raw":
                continue
            mapping = inv.get(k)
            if mapping is None:
                raise BorderlandsError("Unknown key %r in data" % (k, ))
            elif type(mapping) is int:
                pbdata[mapping] = [[self.guess_wire_type(value), value]]
                continue
            key, repeated, child_inv = mapping
            if child_inv is None:
                value = [value] if not repeated else value
                pbdata[key] = [[self.guess_wire_type(v), v] for v in value]
            elif type(child_inv) is int:
                if repeated:
                    b = StringIO()
                    for v in value:
                        self.write_protobuf_value(b, child_inv, v)
                    pbdata[key] = [[2, b.getvalue()]]
                else:
                    pbdata[key] = [[child_inv, value]]
            elif type(child_inv) is tuple:
                value = [value] if not repeated else value
                values = []
                for v in map(child_inv[1], value):
                    if type(v) is list:
                        values.append(v)
                    else:
                        values.append([self.guess_wire_type(v), v])
                pbdata[key] = values
            elif type(child_inv) is dict:
                value = [value] if not repeated else value
                values = []
                for d in [self.remove_structure(v, child_inv) for v in value]:
                    values.append([2, self.write_protobuf(d)])
                pbdata[key] = values
            else:
                raise Exception("Invalid mapping %r for %r: %r" % (mapping, k, value))
        return pbdata

    def guess_wire_type(self, value):
        return 2 if isinstance(value, basestring) else 0

    def invert_structure(self, structure):
        inv = {}
        for k, v in structure.items():
            if type(v) is tuple:
                if type(v[2]) is dict:
                    inv[v[0]] = (k, v[1], self.invert_structure(v[2]))
                else:
                    inv[v[0]] = (k, ) + v[1: ]
            else:
                inv[v] = k
        return inv

    def unwrap_bytes(self, value):
        return [ord(d) for d in value]

    def wrap_bytes(self, value):
        return "".join(map(chr, value))

    def unwrap_float(self, v):
        return struct.unpack("<f", struct.pack("<I", v))[0]

    def wrap_float(self, v):
        return [5, struct.unpack("<I", struct.pack("<f", v))[0]]

    def unwrap_black_market(self, value):
        sdus = self.read_repeated_protobuf_value(value, 0)
        return dict(zip(self.black_market_keys, sdus))

    def wrap_black_market(self, value):
        sdus = [value[k] for k in self.black_market_keys[: len(value)]]
        return self.write_repeated_protobuf_value(sdus, 0)

    def unwrap_challenges(self, data):
        """
        Unwraps our challenge data.  The first ten bytes are a header:

            int32: Unknown, is always "4" on my savegames, though.
            int32: Size in bytes of all the challenges, plus two more bytes
                   for the next short
            short: Number of challenges

        Each challenge takes up a total of 12 bytes, so num_challenges*12
        should always equal size_in_bytes-2.

        The structure of each challenge is:

            byte: unknown, possibly at least part of an ID, but not unique
                  on its own
            byte: unknown, but is always (on my saves, anyway) 6 or 7.
            byte: unknown, but is always 1.
            int32: total value of the challenge, across all resets
            byte: unknown, but is always 1
            int32: previous, pre-challenge-reset value.  Will always be 0
                   until challenges have been reset at least once.

        The first two bytes of each challenge can be taken together, and if so, can
        serve as a unique identifier for the challenge.  I decided to read them in
        that way, as a short value.  I wasn't able to glean any pattern to whether
        a 6 or a 7 shows up in the second byte.

        Once your challenges have been reset in-game, the previous value is copied
        into that second int32, but the total value itself remains unchanged, so at
        that point you need to subtract previous_value from total_value to find the
        actual current state of the challenge (that procedure is obviously true
        prior to any resets, too, since previous_value is just zero in that case).

        It's also worth mentioning that challenge data keeps accumulating even
        after the challenge itself is completed, so the number displayed in-game
        for completed challenges is no longer accurate.

        """
        
        challenges = self.challenges

        (unknown, size_in_bytes, num_challenges) = struct.unpack('%sIIH' % (self.config.endian), data[:10])
        mydict = {'unknown': unknown}

        # Sanity check on size reported
        if (size_in_bytes + 8) != len(data):
            raise BorderlandsError('Challenge data reported as %d bytes, but %d bytes found' % (
                size_in_bytes, len(data)-8))
        
        # Sanity check on number of challenges reported
        if (num_challenges * 12) != (size_in_bytes - 2):
            raise BorderlandsError('%d challenges reported, but %d bytes of data found' % (
                num_challenges, size_in_bytes - 2))

        # Now read them in
        mydict['challenges'] = []
        for challenge in range(num_challenges):
            idx = 10+(challenge*12)
            challenge_dict = dict(zip(
                ['id', 'first_one', 'total_value', 'second_one', 'previous_value'],
                struct.unpack('%sHBIBI' % (self.config.endian), data[idx:idx+12])))
            mydict['challenges'].append(challenge_dict)

            if challenge_dict['id'] in challenges:
                info = challenges[challenge_dict['id']]
                challenge_dict['_id_text'] = info.id_text
                challenge_dict['_category'] = info.cat.name
                challenge_dict['_name'] = info.name
                challenge_dict['_description'] = info.description

        # Return
        return mydict

    def wrap_challenges(self, data):
        """
        Re-wrap our challenge data.  See the notes above in unwrap_challenges for
        details on the structure.

        Note that we are trusting that the correct number of challenges are present
        in our data structure and setting size_in_bytes and num_challenges to match.
        Change the number of challenges at your own risk!
        """
        
        parts = []
        parts.append(struct.pack('%sIIH' % (self.config.endian), data['unknown'], (len(data['challenges'])*12)+2, len(data['challenges'])))
        save_challenges = data['challenges']
        for challenge in save_challenges:
            parts.append(struct.pack('%sHBIBI' % (self.config.endian), challenge['id'],
                challenge['first_one'],
                challenge['total_value'],
                challenge['second_one'],
                challenge['previous_value']))
        return ''.join(parts)

    def unwrap_item_info(self, value):
        is_weapon, item, key = self.unwrap_item(value)
        data = {
            "is_weapon": is_weapon,
            "key": key,
            "set": item[0],
            "level": [item[4], item[5]]
        }
        for i, (k, bits) in enumerate(self.item_header_sizes[is_weapon]):
            lib = item[1 + i] >> bits
            asset = item[1 + i] &~ (lib << bits)
            data[k] = {"lib": lib, "asset": asset}
        bits = 10 + is_weapon
        parts = []
        for value in item[6: ]:
            if value is None:
                parts.append(None)
            else:
                lib = value >> bits
                asset = value &~ (lib << bits)
                parts.append({"lib": lib, "asset": asset})
        data["parts"] = parts
        return data

    def wrap_item_info(self, value):
        item = [value["set"]]
        for key, bits in self.item_header_sizes[value["is_weapon"]]:
            v = value[key]
            item.append((v["lib"] << bits) | v["asset"])
        item.extend(value["level"])
        bits = 10 + value["is_weapon"]
        for v in value["parts"]:
            if v is None:
                item.append(None)
            else:
                item.append((v["lib"] << bits) | v["asset"])
        return self.wrap_item(value["is_weapon"], item, value["key"])


    def unwrap_player_data(self, data):
        """
        The endianness on the few struct calls here appears to actually be
        hardcoded regardless of platform, so we're perhaps just leaving
        them, rather than using self.config.endian as we're doing elsewhere.
        I suspect this might actually be wrong, though, and just happens to
        work.
        """
        if data[: 4] == "CON ":
            raise BorderlandsError("You need to use a program like Horizon or Modio to extract the SaveGame.sav file first")

        if data[: 20] != hashlib.sha1(data[20: ]).digest():
            raise BorderlandsError("Invalid save file")

        data = self.lzo1x_decompress("\xf0" + data[20: ])
        size, wsg, version = struct.unpack(">I3sI", data[: 11])
        if version != 2 and version != 0x02000000:
            raise BorderlandsError("Unknown save version " + str(version))

        if version == 2:
            crc, size = struct.unpack(">II", data[11: 19])
        else:
            crc, size = struct.unpack("<II", data[11: 19])

        bitstream = ReadBitstream(data[19: ])
        tree = self.read_huffman_tree(bitstream)
        player = self.huffman_decompress(tree, bitstream, size)

        if (binascii.crc32(player) & 0xffffffff) != crc:
            raise BorderlandsError("CRC check failed")

        return player

    def wrap_player_data(self, player):
        """
        There's one call in here which had a hard-coded endian, as with
        unwrap_player_data above, so we're leaving that hardcoded for now.
        I suspect that it's wrong to be doing so, though.
        """
        crc = binascii.crc32(player) & 0xffffffff

        bitstream = WriteBitstream()
        tree = self.make_huffman_tree(player)
        self.write_huffman_tree(tree, bitstream)
        self.huffman_compress(self.invert_tree(tree), player, bitstream)
        data = bitstream.getvalue() + "\x00\x00\x00\x00"

        header = struct.pack(">I3s", len(data) + 15, "WSG")
        header = header + struct.pack("%sIII" % (self.config.endian), 2, crc, len(player))

        data = self.lzo1x_1_compress(header + data)[1: ]

        return hashlib.sha1(data).digest() + data

    def expand_zeroes(self, src, ip, extra):
        start = ip
        while src[ip] == 0:
            ip = ip + 1
        v = ((ip - start) * 255) + src[ip]
        return v + extra, ip + 1

    def copy_earlier(self, b, offset, n):
        i = len(b) - offset
        end = i + n
        while i < end:
            chunk = b[i: i + n]
            i = i + len(chunk)
            n = n - len(chunk)
            b.extend(chunk)

    def lzo1x_decompress(self, s):
        dst = bytearray()
        src = bytearray(s)
        ip = 5

        t = src[ip]; ip += 1
        if t > 17:
            t = t - 17
            dst.extend(src[ip: ip + t]); ip += t
            t = src[ip]; ip += 1
        elif t < 16:
            if t == 0:
                t, ip = self.expand_zeroes(src, ip, 15)
            dst.extend(src[ip: ip + t + 3]); ip += t + 3
            t = src[ip]; ip += 1

        while 1:
            while 1:
                if t >= 64:
                    self.copy_earlier(dst, 1 + ((t >> 2) & 7) + (src[ip] << 3), (t >> 5) + 1); ip += 1
                elif t >= 32:
                    count = t & 31
                    if count == 0:
                        count, ip = self.expand_zeroes(src, ip, 31)
                    t = src[ip]
                    self.copy_earlier(dst, 1 + ((t | (src[ip + 1] << 8)) >> 2), count + 2); ip += 2
                elif t >= 16:
                    offset = (t & 8) << 11
                    count = t & 7
                    if count == 0:
                        count, ip = self.expand_zeroes(src, ip, 7)
                    t = src[ip]
                    offset += (t | (src[ip + 1] << 8)) >> 2; ip += 2
                    if offset == 0:
                        return str(dst)
                    self.copy_earlier(dst, offset + 0x4000, count + 2)
                else:
                    self.copy_earlier(dst, 1 + (t >> 2) + (src[ip] << 2), 2); ip += 1

                t = t & 3
                if t == 0:
                    break
                dst.extend(src[ip: ip + t]); ip += t
                t = src[ip]; ip += 1

            while 1:
                t = src[ip]; ip += 1
                if t < 16:
                    if t == 0:
                        t, ip = self.expand_zeroes(src, ip, 15)
                    dst.extend(src[ip: ip + t + 3]); ip += t + 3
                    t = src[ip]; ip += 1
                if t < 16:
                    self.copy_earlier(dst, 1 + 0x0800 + (t >> 2) + (src[ip] << 2), 3); ip += 1
                    t = t & 3
                    if t == 0:
                        continue
                    dst.extend(src[ip: ip + t]); ip += t
                    t = src[ip]; ip += 1
                break

    def read_xor32(self, src, p1, p2):
        v1 = src[p1] | (src[p1 + 1] << 8) | (src[p1 + 2] << 16) | (src[p1 + 3] << 24)
        v2 = src[p2] | (src[p2 + 1] << 8) | (src[p2 + 2] << 16) | (src[p2 + 3] << 24)
        return v1 ^ v2

    def lzo1x_1_compress_core(self, src, dst, ti, ip_start, ip_len):
        dict_entries = [0] * 16384

        in_end = ip_start + ip_len
        ip_end = ip_start + ip_len - 20

        ip = ip_start
        ii = ip_start

        ip += (4 - ti) if ti < 4 else 0
        ip += 1 + ((ip - ii) >> 5)
        while 1:
            while 1:
                if ip >= ip_end:
                    return in_end - (ii - ti)
                dv = src[ip: ip + 4]
                dindex = dv[0] | (dv[1] << 8) | (dv[2] << 16) | (dv[3] << 24)
                dindex = ((0x1824429d * dindex) >> 18) & 0x3fff
                m_pos = ip_start + dict_entries[dindex]
                dict_entries[dindex] = (ip - ip_start) & 0xffff
                if dv == src[m_pos: m_pos + 4]:
                    break
                ip += 1 + ((ip - ii) >> 5)

            ii -= ti; ti = 0
            t = ip - ii
            if t != 0:
                if t <= 3:
                    dst[-2] |= t
                    dst.extend(src[ii: ii + t])
                elif t <= 16:
                    dst.append(t - 3)
                    dst.extend(src[ii: ii + t])
                else:
                    if t <= 18:
                        dst.append(t - 3)
                    else:
                        tt = t - 18
                        dst.append(0)
                        n, tt = divmod(tt, 255)
                        dst.extend("\x00" * n)
                        dst.append(tt)
                    dst.extend(src[ii: ii + t])
                    ii += t

            m_len = 4
            v = self.read_xor32(src, ip + m_len, m_pos + m_len)
            if v == 0:
                while 1:
                    m_len += 4
                    v = self.read_xor32(src, ip + m_len, m_pos + m_len)
                    if ip + m_len >= ip_end:
                        break
                    elif v != 0:
                        m_len += self.clz_table[(v & -v) % 37] >> 3
                        break
            else:
                m_len += self.clz_table[(v & -v) % 37] >> 3

            m_off = ip - m_pos
            ip += m_len
            ii = ip
            if m_len <= 8 and m_off <= 0x0800:
                m_off -= 1
                dst.append(((m_len - 1) << 5) | ((m_off & 7) << 2))
                dst.append(m_off >> 3)
            elif m_off <= 0x4000:
                m_off -= 1
                if m_len <= 33:
                    dst.append(32 | (m_len - 2))
                else:
                    m_len -= 33
                    dst.append(32)
                    n, m_len = divmod(m_len, 255)
                    dst.extend("\x00" * n)
                    dst.append(m_len)
                dst.append((m_off << 2) & 0xff)
                dst.append((m_off >> 6) & 0xff)
            else:
                m_off -= 0x4000
                if m_len <= 9:
                    dst.append(0xff & (16 | ((m_off >> 11) & 8) | (m_len - 2)))
                else:
                    m_len -= 9
                    dst.append(0xff & (16 | ((m_off >> 11) & 8)))
                    n, m_len = divmod(m_len, 255)
                    dst.extend("\x00" * n)
                    dst.append(m_len)
                dst.append((m_off << 2) & 0xff)
                dst.append((m_off >> 6) & 0xff)

    def lzo1x_1_compress(self, s):
        src = bytearray(s)
        dst = bytearray()

        ip = 0
        l = len(s)
        t = 0

        dst.append(240)
        dst.append((l >> 24) & 0xff)
        dst.append((l >> 16) & 0xff)
        dst.append((l >>  8) & 0xff)
        dst.append( l        & 0xff)

        while l > 20 and t + l > 31:
            ll = min(49152, l)
            t = self.lzo1x_1_compress_core(src, dst, t, ip, ll)
            ip += ll
            l -= ll
        t += l

        if t > 0:
            ii = len(s) - t

            if len(dst) == 5 and t <= 238:
                dst.append(17 + t)
            elif t <= 3:
                dst[-2] |= t
            elif t <= 18:
                dst.append(t - 3)
            else:
                tt = t - 18
                dst.append(0)
                n, tt = divmod(tt, 255)
                dst.extend("\x00" * n)
                dst.append(tt)
            dst.extend(src[ii: ii + t])

        dst.append(16 | 1)
        dst.append(0)
        dst.append(0)

        return str(dst)

    def modify_save(self, data):
        """
        Performs a set of modifications on file data, based on our
        config object.  "data" should be the raw data from a save
        file.
        """

        player = self.read_protobuf(self.unwrap_player_data(data))
        save_structure = self.save_structure
        config = self.config

        if config.level is not None:
            self.debug(' - Updating to level %d' % (config.level))
            lower = int(60 * (config.level ** 2.8) - 59.2)
            upper = int(60 * ((config.level + 1) ** 2.8) - 59.2)
            if player[3][0][1] not in range(lower, upper):
                player[3][0][1] = lower
                self.debug('   - Also updating XP to %d' % (lower))
            player[2] = [[0, config.level]]

        if any([x is not None for x in [config.money, config.eridium, config.moonstone, config.seraph, config.torgue]]):
            raw = player[6][0][1]
            b = StringIO(raw)
            values = []
            while b.tell() < len(raw):
                values.append(self.read_protobuf_value(b, 0))
            if config.money is not None:
                self.debug(' - Setting available money to %d' % (config.money))
                values[0] = config.money
            if config.eridium is not None:
                self.debug(' - Setting available eridium to %d' % (config.eridium))
                values[1] = config.eridium
            if config.moonstone is not None:
                self.debug(' - Setting available moonstone to %d' % (config.moonstone))
                values[1] = config.moonstone
            if config.seraph is not None:
                self.debug(' - Setting available Seraph Crystals to %d' % (config.seraph))
                values[2] = config.seraph
            if config.torgue is not None:
                self.debug(' - Setting available Torgue Tokens to %d' % (config.torgue))
                values[4] = config.torgue
            player[6][0] = [0, values]

        # Note that this block should always come *after* the block which sets
        # character level, in case we've been instructed to set items to the
        # character's level.
        if config.itemlevels is not None:
            if config.itemlevels > 0:
                self.debug(' - Setting all items to level %d' % (config.itemlevels))
                level = config.itemlevels
            else:
                level = player[2][0][1]
                self.debug(' - Setting all items to character level (%d)' % (level))
            for field_number in (53, 54):
                for field in player[field_number]:
                    field_data = self.read_protobuf(field[1])
                    is_weapon, item, key = self.unwrap_item(field_data[1][0][1])
                    if item[4] > 1:
                        item = item[: 4] + [level, level] + item[6: ]
                        field_data[1][0][1] = self.wrap_item(is_weapon, item, key)
                        field[1] = self.write_protobuf(field_data)

        if config.backpack is not None:
            self.debug(' - Setting backpack size to %d' % (config.backpack))
            size = config.backpack
            sdus = int(math.ceil((size - self.min_backpack_size) / 3.0))
            self.debug('   - Setting SDU size to %d' % (sdus))
            new_size = self.min_backpack_size + (sdus * 3)
            if size != new_size:
                self.debug('   - Resetting backpack size to %d to match SDU count' % (new_size))
            slots = self.read_protobuf(player[13][0][1])
            slots[1][0][1] = new_size
            player[13][0][1] = self.write_protobuf(slots)
            s = self.read_repeated_protobuf_value(player[36][0][1], 0)
            player[36][0][1] = self.write_repeated_protobuf_value(s[: 7] + [sdus] + s[8: ], 0)

        if config.bank is not None:
            self.debug(' - Setting bank size to %d' % (config.bank))
            size = config.bank
            sdus = int(min(255, math.ceil((size - self.min_bank_size) / 2.0)))
            self.debug('   - Setting SDU size to %d' % (sdus))
            new_size = self.min_bank_size + (sdus * 2)
            if size != new_size:
                self.debug('   - Resetting bank size to %d to match SDU count' % (new_size))
            if player.has_key(56):
                player[56][0][1] = new_size
            else:
                player[56] = [[0, new_size]]
            s = self.read_repeated_protobuf_value(player[36][0][1], 0)
            if len(s) < 9:
                s = s + (9 - len(s)) * [0]
            player[36][0][1] = self.write_repeated_protobuf_value(s[: 8] + [sdus] + s[9: ], 0)

        if config.gunslots is not None:
            self.debug(' - Setting available gun slots to %d' % (config.gunslots))
            n = config.gunslots
            slots = self.read_protobuf(player[13][0][1])
            slots[2][0][1] = n
            if slots[3][0][1] > n - 2:
                slots[3][0][1] = n - 2
            player[13][0][1] = self.write_protobuf(slots)

        if len(config.unlock) > 0:
            if 'slaughterdome' in config.unlock:
                unlocked, notifications = [], []
                if player.has_key(23):
                    unlocked = map(ord, player[23][0][1])
                if player.has_key(24):
                    notifications = map(ord, player[24][0][1])
                self.debug(' - Unlocking Creature Slaughterdome')
                if 1 not in unlocked:
                    unlocked.append(1)
                if 1 not in notifications:
                    notifications.append(1)
                if unlocked:
                    player[23] = [[2, "".join(map(chr, unlocked))]]
                if notifications:
                    player[24] = [[2, "".join(map(chr, notifications))]]
            if 'tvhm' in config.unlock:
                self.debug(' - Unlocking TVHM')
                if player[7][0][1] < 1:
                    player[7][0][1] = 1
            if 'challenges' in config.unlock:
                self.debug(' - Unlocking all non-level-specific challenges')
                challenge_unlocks = [self.apply_structure(self.read_protobuf(d[1]), save_structure[38][2]) for d in player[38]]
                inverted_structure = self.invert_structure(save_structure[38][2])
                seen_challenges = {}
                for unlock in challenge_unlocks:
                    seen_challenges[unlock['name']] = True
                for challenge in self.challenges.values():
                    if challenge.id_text not in seen_challenges:
                        player[38].append([2, self.write_protobuf(self.remove_structure(dict([
                            ('dlc_id', challenge.cat.dlc),
                            ('is_from_dlc', challenge.cat.is_from_dlc),
                            ('name', challenge.id_text)]), inverted_structure))])
            if 'ammo' in config.unlock:
                self.debug(' - Unlocking ammo capacity')
                s = self.read_repeated_protobuf_value(player[36][0][1], 0)
                for idx, (key, value) in enumerate(zip(self.black_market_keys, s)):
                    if key in self.black_market_ammo:
                        s[idx] = 7
                player[36][0][1] = self.write_repeated_protobuf_value(s, 0)

        # This should always come after the ammo-unlock section, since our
        # max ammo will change if more black market SDUs are unlocked.
        if config.maxammo is not None:
            self.debug(' - Setting ammo pools to maximum')

            # First we've gotta figure out our black market levels
            s = self.read_repeated_protobuf_value(player[36][0][1], 0)
            bm_levels = dict(zip(self.black_market_keys, s))

            # Make a dict of what our max ammo is for each of our black market
            # ammo pools
            max_ammo = {}
            for ammo_type, ammo_level in bm_levels.items():
                if ammo_type in self.black_market_ammo:
                    ammo_values = self.black_market_ammo[ammo_type]
                    if len(ammo_values) - 1 < ammo_level:
                        max_ammo[ammo_type] = (len(ammo_values)-1, ammo_values[-1])
                    else:
                        max_ammo[ammo_type] = (ammo_level, ammo_values[ammo_level])

            # Now loop through our 'resources' structure and modify to
            # suit, updating 'amount' and 'level' as we go.
            inverted_structure = self.invert_structure(save_structure[11][2])
            seen_ammo = {}
            for idx, protobuf in enumerate(player[11]):
                data = self.apply_structure(self.read_protobuf(protobuf[1]), save_structure[11][2])
                if data['resource'] in self.ammo_resource_lookup:
                    ammo_type = self.ammo_resource_lookup[data['resource']]
                    seen_ammo[ammo_type] = True
                    if ammo_type in max_ammo:

                        # Set the data in the structure
                        data['level'] = max_ammo[ammo_type][0]
                        data['amount'] = float(max_ammo[ammo_type][1])

                        # And now convert back into a protobuf
                        player[11][idx][1] = self.write_protobuf(self.remove_structure(data, inverted_structure))

                    else:
                        self.error('Ammo type "%s" / pool "%s" not found!' % (ammo_type, data['pool']))
                else:
                    self.error('Ammo pool "%s" not found!' % (data['pool']))

            # Also, early in the game there isn't an entry in here for, for instance,
            # rocket launchers.  So let's make sure that all our known ammo exists.
            for ammo_type in bm_levels.keys():
                if ammo_type in self.ammo_resources.keys() and ammo_type not in seen_ammo:
                    new_struct = {
                        'resource': self.ammo_resources[ammo_type][0],
                        'pool': self.ammo_resources[ammo_type][1],
                        'level': max_ammo[ammo_type][0],
                        'amount': float(max_ammo[ammo_type][1]),
                    }
                    player[11].append([2, self.write_protobuf(self.remove_structure(new_struct, inverted_structure))])

        if len(config.challenges) > 0:
            data = self.unwrap_challenges(player[15][0][1])
            # You can specify multiple options at once.  Specifying "max" and
            # "bonus" at the same time, for instance, will put everything at its
            # max value, and then potentially lower the ones which have bonuses.
            do_zero = 'zero' in config.challenges
            do_max = 'max' in config.challenges
            do_bonus = 'bonus' in config.challenges

            if any([do_zero, do_max, do_bonus]):
                self.debug(' - Working with challenge data:')
                if do_zero:
                    self.debug('   - Setting challenges to 0')
                if do_max:
                    self.debug('   - Setting challenges to max-1')
                if do_bonus:
                    self.debug('   - Setting bonus challenges')

            # Loop through
            for save_challenge in data['challenges']:
                if save_challenge['id'] in self.challenges:
                    if do_zero:
                        save_challenge['total_value'] = save_challenge['previous_value']
                    if do_max:
                        save_challenge['total_value'] = save_challenge['previous_value'] + self.challenges[save_challenge['id']].get_max()
                    if do_bonus and self.challenges[save_challenge['id']].bonus:
                        bonus_value = save_challenge['previous_value'] + self.challenges[save_challenge['id']].get_bonus()
                        if do_max or do_zero or save_challenge['total_value'] < bonus_value:
                            save_challenge['total_value'] = bonus_value

            # Re-wrap the data
            player[15][0][1] = self.wrap_challenges(data)

        if config.name is not None and len(config.name) > 0:
            self.debug(' - Setting character name to "%s"' % (config.name))
            data = self.apply_structure(self.read_protobuf(player[19][0][1]), save_structure[19][2])
            data['name'] = config.name
            player[19][0][1] = self.write_protobuf(self.remove_structure(data, self.invert_structure(save_structure[19][2])))

        if config.save_game_id is not None and config.save_game_id > 0:
            self.debug(' - Setting save slot ID to %d' % (config.save_game_id))
            player[20][0][1] = config.save_game_id

        return self.wrap_player_data(self.write_protobuf(player))

    def export_items(self, data, output, extended_output):
        """
        Exports items stored in savegame data 'data' to the open
        filehandle 'output'
        """
        player = self.read_protobuf(self.unwrap_player_data(data))
        skipped_count = 0
        assets = AssetLibrary() if extended_output else None
        for i, name in ((41, "Bank"), (53, "Items"), (54, "Weapons")):
            count = 0
            content = player.get(i)
            if content is None:
                continue
            print >>output, "; " + name
            for field in content:
                raw = self.read_protobuf(field[1])[1][0][1]

                # For whatever reason, Borderlands seems to sometimes store items
                # which show up as [255, 0, 0, 0, ..., 0] in "item" after being
                # passed to unwrap_item().  Basically an "empty" item which doesn't
                # actually show up ingame (or in Gibbed).  Their base64
                # representations end up looking like this:
                #
                #   BL2(CgAAAABs+/8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==)
                #
                # There always seems to be at least a couple of them, and there's
                # no real reason to export them since they're not real items and
                # don't contain any information.  So, we're going to skip them.
                is_weapon, item, key = self.unwrap_item(raw)
                item_info = self.unwrap_item_info(raw)
                if item[0] == 255 and not any([val != 0 for val in item[1:]]):
                    skipped_count += 1
                else:
                    count += 1
                    raw = self.replace_raw_item_key(raw, 0)
                    code = '%s(%s)' % (self.item_prefix, raw.encode("base64").strip())
                    if extended_output:
                        set_id = item_info['set']
                        if is_weapon:
                            type_lookup = assets.get_weapon_type
                            part_lookup = assets.get_weapon_part
                        else:
                            type_lookup = assets.get_item_type
                            part_lookup = assets.get_item_part
                        data = [
                            '',
                            str(set_id),
                            'weapon' if item_info['is_weapon'] else 'item',
                            assets.get_balance_type(set_id, item_info['balance']),
                            type_lookup(set_id, item_info['type']),
                            str(item_info['level'][1]),
                            assets.get_manufacturer(set_id, item_info['manufacturer']),
                        ] + [part_lookup(set_id, part) for part in item_info['parts']]
                        #data = ['.'.join(x.split('.')[-2:]) for x in data]
                        extra = ','.join(data)
                    else:
                        extra = ''
                    print >>output, code + extra
            self.debug(' - %s exported: %d' % (name, count))
        self.debug(' - Empty items skipped: %d' % (skipped_count))

    def import_items(self, data, codelist):
        """
        Imports items into savegame data "data" based on the passed-in
        item list in "codelist"
        """
        player = self.read_protobuf(self.unwrap_player_data(data))

        prefix_length = len(self.item_prefix)+1

        bank_count = 0
        weapon_count = 0
        item_count = 0

        to_bank = False
        for line in codelist.splitlines():
            line = line.strip()
            if line.startswith(";"):
                name = line[1: ].strip().lower()
                if name == "bank":
                    to_bank = True
                elif name in ("items", "weapons"):
                    to_bank = False
                continue
            elif line[: prefix_length] + line[-1: ] != '%s()' % (self.item_prefix):
                continue

            code = line[prefix_length: -1]
            try:
                raw = code.decode("base64")
            except binascii.Error:
                continue

            key = random.randrange(0x100000000) - 0x80000000
            raw = self.replace_raw_item_key(raw, key)
            if to_bank:
                bank_count += 1
                field = 41
                entry = {1: [[2, raw]]}
            elif (ord(raw[0]) & 0x80) == 0:
                item_count += 1
                field = 53
                entry = {1: [[2, raw]], 2: [[0, 1]], 3: [[0, 0]], 4: [[0, 1]]}
            else:
                weapon_count += 1
                field = 54
                entry = {1: [[2, raw]], 2: [[0, 0]], 3: [[0, 1]]}

            player.setdefault(field, []).append([2, self.write_protobuf(entry)])

        self.debug(' - Bank imported: %d' % (bank_count))
        self.debug(' - Items imported: %d' % (item_count))
        self.debug(' - Weapons imported: %d' % (weapon_count))

        return self.wrap_player_data(self.write_protobuf(player))

    def __init__(self, args):
        """
        Constructor.  Parses arguments and sets up our save_structure
        struct.
        """

        # Set up a reverse lookup for our ammo pools
        self.ammo_resource_lookup = {}
        for shortname, (resource, pool) in self.ammo_resources.items():
            self.ammo_resource_lookup[resource] = shortname
        
        # Parse Arguments
        self.parse_args(args)

        # This is implemented in AppBL2 and AppBLTPS
        self.setup_save_structure()

    def parse_args(self, argv):
        """
        Parse our arguments.
        """

        # Set up our config object
        self.config = Config()
        config = self.config

        parser = argparse.ArgumentParser(description='Modify %s Save Files' % (self.game_name),
                formatter_class=argparse.ArgumentDefaultsHelpFormatter)

        # Optional args

        parser.add_argument('-o', '--output',
                choices=['savegame', 'decoded', 'decodedjson', 'json', 'items', 'itemscsv'],
                default='savegame',
                help='Output file format.  The most useful to humans are: savegame, json, and items',
                )

        parser.add_argument('-i', '--import-items',
                dest='import_items',
                help='read in codes for items and add them to the bank and inventory',
                )

        parser.add_argument('-j', '--json',
                action='store_true',
                help='read savegame data from JSON format, rather than savegame',
                )

        parser.add_argument('-b', '--bigendian',
                action='store_true',
                help='change the output format to big-endian, to write PS/xbox save files',
                )

        parser.add_argument('-q', '--quiet',
                dest='verbose',
                action='store_false',
                help='quiet output (should generate no output unless there are errors)',
                )

        parser.add_argument('-f', '--force',
                action='store_true',
                help='force output file overwrite, if the destination file exists',
                )

        # More optional args - used to be the "modify" option

        parser.add_argument('--name',
                help='Set the name of the character',
                )

        parser.add_argument('--save-game-id',
                dest='save_game_id',
                type=int,
                help='Set the save game slot ID of the character (probably not actually needed ever)',
                )

        parser.add_argument('--level',
                type=int,
                help='Set the character to this level',
                )

        parser.add_argument('--money',
                type=int,
                help='Money to set for character',
                )

        # B2 and TPS have different currency types, so this function is
        # implemented in the implementing classes.
        self.setup_currency_args(parser)

        parser.add_argument('--itemlevels',
                type=int,
                help='Set item levels (to set to current player level, specify 0)',
                )

        parser.add_argument('--backpack',
                help='Set size of backpack (maximum is %d, "max" may be specified)' % (self.max_backpack_size),
                )

        parser.add_argument('--bank',
                help='Set size of bank(maximum is %d, "max" may be specified)' % (self.max_bank_size),
                )

        parser.add_argument('--gunslots',
                type=int,
                choices=[2,3,4],
                help='Set number of gun slots open',
                )

        parser.add_argument('--unlock',
                action=DictAction,
                choices=self.unlock_choices,
                default={},
                help='Game features to unlock',
                )

        parser.add_argument('--challenges',
                action=DictAction,
                choices=['zero', 'max', 'bonus'],
                default={},
                help='Levels to set on challenge data',
                )

        parser.add_argument('--maxammo',
                action='store_true',
                help='Fill all ammo pools to their maximum',
                )

        # Positional args

        parser.add_argument('input_filename',
                help='Input filename, can be "-" to specify STDIN'
                )

        parser.add_argument('output_filename',
                help='Output filename, can be "-" to specify STDOUT'
                )

        # Actually parse the args
        parser.parse_args(argv, config)

        # Do some extra fiddling
        config.finish(parser, self)

    def notice(self, output):
        """
        Stupid little function to send some output to STDERR.
        """
        print >>sys.stderr, output

    def error(self, output):
        """
        Stupid little function to send some output to STDERR.
        """
        print >>sys.stderr, 'ERROR: %s' % (output)

    def debug(self, output):
        """
        Stupid little function to send some output to STDERR.
        """
        if self.config.verbose:
            print >>sys.stderr, output

    def run(self):
        """
        Main routine - loads data, does things to it, and then writes
        out a file.
        """

        config = self.config

        # Open up our input file
        self.debug('')
        if config.input_filename == '-':
            self.debug('Using STDIN for input file')
            input_file = sys.stdin
        else:
            self.debug('Opening %s for input file' % (config.input_filename))
            input_file = open(config.input_filename, 'rb')
        self.debug('')

        # ... and read it in.
        save_data = input_file.read()
        if config.input_filename != '-':
            input_file.close()

        # If we're reading from JSON, convert it
        if config.json:
            self.debug('Interpreting JSON data')
            data = json.loads(save_data, encoding='latin1')
            if not data.has_key('1'):
                # This means the file had been output as 'json'
                data = self.remove_structure(data, self.invert_structure(self.save_structure))
            save_data = self.wrap_player_data(self.write_protobuf(data))

        # If we've been told to import items, do so.
        if config.import_items:
            self.debug('Importing items from %s' % (config.import_items))
            itemlist = open(config.import_items, 'r')
            save_data = self.import_items(save_data, itemlist.read())
            itemlist.close()

        # Now perform any changes, if requested
        if config.changes:
            self.debug('Performing requested changes')
            save_data = self.modify_save(save_data)

        # Open our output file
        self.debug('')
        if config.output_filename == '-':
            self.debug('Using STDOUT for output file')
            output_file = sys.stdout
        else:
            self.debug('Opening %s for output file' % (config.output_filename))
            if os.path.exists(config.output_filename):
                if config.force:
                    self.debug('Overwriting output file "%s"' % (config.output_filename))
                else:
                    if config.input_filename == '-':
                        raise BorderlandsError('Output filename "%s" exists and --force not specified, aborting' %
                            (config.output_filename))
                    else:
                        self.notice('')
                        self.notice('Output filename "%s" exists' % (config.output_filename))
                        sys.stderr.write('Continue and overwrite? [y|N] ')
                        answer = sys.stdin.readline()
                        if answer[0].lower() == 'y':
                            self.notice('')
                            self.notice('Continuing!')
                        else:
                            self.notice('')
                            self.notice('Exiting!')
                            return
            output_file = open(config.output_filename, 'wb')

        # Now output based on what we've been told to do
        if config.output == 'items' or config.output == 'itemscsv':
            self.debug('Exporting items')
            self.export_items(save_data, output_file, extended_output=config.output == 'itemscsv')
        elif config.output == 'savegame':
            self.debug('Writing savegame file')
            output_file.write(save_data)
        else:
            self.debug('Preparing decoded savegame file')
            player = self.unwrap_player_data(save_data)
            if config.output == 'decodedjson' or config.output == 'json':
                self.debug('Converting to JSON for more human-readable output')
                data = self.read_protobuf(player)
                if config.output == 'json':
                    self.debug('Parsing protobuf data for even more human-readable output')
                    data = self.apply_structure(data, self.save_structure)
                player = json.dumps(data, encoding="latin1", sort_keys=True, indent=4)
            self.debug('Writing decoded savegame file')
            output_file.write(player)

        # Close the output file
        if config.output_filename != '-':
            output_file.close()

        # ... aaand we're done.
        self.debug('')
        self.debug('Done!')
