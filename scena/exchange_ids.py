"""Persistent identities shared by placement copying and scene export."""

import re
import uuid

import bpy

KEY = "_scena_exchange_id"
OWNER = KEY + "_owner"


def new_id(block):
    block[KEY] = uuid.uuid4().hex
    block[OWNER] = block.name


def copy_block(original):
    duplicate = original.copy()
    new_id(duplicate)
    return duplicate


def stable_ids(blocks, possible_owners=()):
    requested = set(blocks)
    inherited = {value for block in requested if isinstance(value := block.get(KEY), str)}
    # Native Blender duplicates and older files can carry the original's ID,
    # including when its owner lives in a hidden collection or another scene.
    candidates = requested.copy()
    if inherited:
        for population in possible_owners:
            candidates.update(b for b in population if isinstance(value := b.get(KEY), str)
                              and value in inherited)
    seen = set()
    result = {}
    for block in sorted(candidates, key=lambda b: (not b.library, b.get(OWNER) != b.name, b.session_uid)):
        value = block.get(KEY)
        if block.library:
            value = uuid.uuid5(uuid.NAMESPACE_URL, bpy.path.abspath(block.library.filepath)
                               + "/" + block.bl_rna.identifier + "/" + block.name).hex
        else:
            if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value) or value in seen:
                value = uuid.uuid4().hex
                block[KEY] = value
            if block.get(OWNER) != block.name:
                block[OWNER] = block.name
        seen.add(value)
        if block in requested:
            result[block] = value
    return result
