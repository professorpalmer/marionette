"""Windows dest-into-main shards must interleave stably."""
from conftest import parse_pytest_shard, select_pytest_shard


class _Item:
    def __init__(self, nodeid):
        self.nodeid = nodeid


def test_parse_pytest_shard_rejects_junk():
    assert parse_pytest_shard("") is None
    assert parse_pytest_shard("nope") is None
    assert parse_pytest_shard("0/4") is None
    assert parse_pytest_shard("5/4") is None
    assert parse_pytest_shard("2/4") == (2, 4)


def test_select_pytest_shard_interleaves_without_overlap():
    items = [_Item("tests/test_%02d.py::t" % i) for i in range(8)]
    parts = [select_pytest_shard(items, "%s/4" % n) for n in (1, 2, 3, 4)]
    ids = [[item.nodeid for item in part] for part in parts]
    flat = [nodeid for part in ids for nodeid in part]
    assert len(flat) == 8
    assert len(set(flat)) == 8
    assert ids[0][0].endswith("00.py::t")
    assert ids[1][0].endswith("01.py::t")
