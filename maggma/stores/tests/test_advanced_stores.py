# coding: utf-8
"""
Tests for advanced stores
"""
import time

import os
import shutil
import signal
import subprocess
import tempfile
import pytest

from mongogrant.client import seed, check
from mongogrant.config import Config
from mongogrant import Client
from pymongo import MongoClient
from pymongo.collection import Collection
from unittest.mock import patch
from uuid import uuid4

from maggma.stores import (
    MongoStore,
    MongograntStore,
    VaultStore,
    MemoryStore,
    AliasingStore,
    SandboxStore,
)
from maggma.stores.advanced_stores import substitute



@pytest.fixture("module")
def mgrant_server():
    _, config_path = tempfile.mkstemp()
    _, mdlogpath = tempfile.mkstemp()
    mdpath = tempfile.mkdtemp()
    mdport = 27020
    if not (os.getenv("CONTINUOUS_INTEGRATION") and os.getenv("TRAVIS")):
        basecmd = (
            f"mongod --port {mdport} --dbpath {mdpath} --quiet --logpath {mdlogpath} "
            "--bind_ip_all --auth"
        )
        mongod_process = subprocess.Popen(basecmd, shell=True, start_new_session=True)
        time.sleep(5)
        client = MongoClient(port=mdport)
        client.admin.command(
            "createUser", "mongoadmin", pwd="mongoadminpass", roles=["root"]
        )
        client.close()
    dbname = "test_" + uuid4().hex
    db = MongoClient(f"mongodb://mongoadmin:mongoadminpass@127.0.0.1:{mdport}/admin")[
        dbname
    ]
    db.command("createUser", "reader", pwd="readerpass", roles=["read"])
    db.command("createUser", "writer", pwd="writerpass", roles=["readWrite"])
    db.client.close()

    # Yields the fixture to use
    yield config_path, mdport, dbname

    os.remove(config_path)
    if not (os.getenv("CONTINUOUS_INTEGRATION") and os.getenv("TRAVIS")):
        os.killpg(os.getpgid(mongod_process.pid), signal.SIGTERM)
        os.waitpid(mongod_process.pid, 0)
    shutil.rmtree(mdpath)
    os.remove(mdlogpath)


@pytest.fixture("module")
def mgrant_user(mgrant_server):
    config_path, mdport, dbname = mgrant_server

    config = Config(check=check, path=config_path, seed=seed())
    client = Client(config)
    client.set_auth(
        host=f"localhost:{mdport}",
        db=dbname,
        role="read",
        username="reader",
        password="readerpass",
    )
    client.set_auth(
        host=f"localhost:{mdport}",
        db=dbname,
        role="readWrite",
        username="writer",
        password="writerpass",
    )
    client.set_alias("testhost", f"localhost:{mdport}", which="host")
    client.set_alias("testdb", dbname, which="db")

    return client


def connected_user(store):
    return store._collection.database.command("connectionStatus")["authInfo"][
        "authenticatedUsers"
    ][0]["user"]


def test_mgrant_connect(mgrant_server, mgrant_user):
    config_path, mdport, dbname = mgrant_server
    assert mgrant_user is not None
    store = MongograntStore(
        "ro:testhost/testdb", "tasks", mgclient_config_path=config_path
    )
    store.connect()
    assert isinstance(store._collection, Collection)
    assert connected_user(store) == "reader"
    store = MongograntStore(
        "rw:testhost/testdb", "tasks", mgclient_config_path=config_path
    )
    store.connect()
    assert isinstance(store._collection, Collection)
    assert connected_user(store) == "writer"


def vault_store():
    with patch("hvac.Client") as mock:
        instance = mock.return_value
        instance.auth_github.return_value = True
        instance.is_authenticated.return_value = True
        instance.read.return_value = {
            "wrap_info": None,
            "request_id": "2c72c063-2452-d1cd-19a2-91163c7395f7",
            "data": {
                "value": '{"db": "mg_core_prod", "host": "matgen2.lbl.gov", "username": "test", "password": "pass"}'
            },
            "auth": None,
            "warnings": None,
            "renewable": False,
            "lease_duration": 2764800,
            "lease_id": "",
        }
        v = VaultStore("test_coll", "secret/matgen/maggma")

    return v


def test_vault_init():
    """
    Test initing a vault store using a mock hvac client
    """
    os.environ["VAULT_ADDR"] = "https://fake:8200/"
    os.environ["VAULT_TOKEN"] = "dummy"

    # Just test that we successfully instantiated
    v = vault_store()
    assert isinstance(v, MongoStore)


def test_vault_github_token():
    """
    Test using VaultStore with GITHUB_TOKEN and mock hvac
    """
    # Save token in env
    os.environ["VAULT_ADDR"] = "https://fake:8200/"
    os.environ["GITHUB_TOKEN"] = "dummy"

    v = vault_store()
    # Just test that we successfully instantiated
    assert isinstance(v, MongoStore)


def test_vault_missing_env():
    """
    Test VaultStore should raise an error if environment is not set
    """
    del os.environ["VAULT_TOKEN"]
    del os.environ["VAULT_ADDR"]
    del os.environ["GITHUB_TOKEN"]

    # Create should raise an error
    with pytest.raises(RuntimeError):
        vault_store()


@pytest.fixture
def alias_store():
    memorystore = MemoryStore("test")
    memorystore.connect()
    alias_store = AliasingStore(memorystore, {"a": "b", "c.d": "e", "f": "g.h"})
    return alias_store


def test_aliasing_query(alias_store):

    d = [{"b": 1}, {"e": 2}, {"g": {"h": 3}}]
    alias_store.store._collection.insert_many(d)

    assert "a" in list(alias_store.query(criteria={"a": {"$exists": 1}}))[0]
    assert "c" in list(alias_store.query(criteria={"c.d": {"$exists": 1}}))[0]
    assert "d" in list(alias_store.query(criteria={"c.d": {"$exists": 1}}))[0].get(
        "c", {}
    )
    assert "f" in list(alias_store.query(criteria={"f": {"$exists": 1}}))[0]


def test_aliasing_update(alias_store):

    alias_store.update(
        [
            {"task_id": "mp-3", "a": 4},
            {"task_id": "mp-4", "c": {"d": 5}},
            {"task_id": "mp-5", "f": 6},
        ]
    )
    assert list(alias_store.query(criteria={"task_id": "mp-3"}))[0]["a"] == 4
    assert list(alias_store.query(criteria={"task_id": "mp-4"}))[0]["c"]["d"] == 5
    assert list(alias_store.query(criteria={"task_id": "mp-5"}))[0]["f"] == 6

    assert list(alias_store.store.query(criteria={"task_id": "mp-3"}))[0]["b"] == 4
    assert list(alias_store.store.query(criteria={"task_id": "mp-4"}))[0]["e"] == 5

    assert list(alias_store.store.query(criteria={"task_id": "mp-5"}))[0]["g"]["h"] == 6


def test_aliasing_substitute(alias_store):
    aliases = {"a": "b", "c.d": "e", "f": "g.h"}

    d = {"b": 1}
    substitute(d, aliases)
    assert "a" in d

    d = {"e": 1}
    substitute(d, aliases)
    assert "c" in d
    assert "d" in d.get("c", {})

    d = {"g": {"h": 4}}
    substitute(d, aliases)
    assert "f" in d

    d = None
    substitute(d, aliases)
    assert d is None


@pytest.fixture
def sandbox_store():
    memstore = MemoryStore()
    store = SandboxStore(memstore, sandbox="test")
    store.connect()
    return store


def test_sandbox_query(sandbox_store):
    sandbox_store.collection.insert_one({"a": 1, "b": 2, "c": 3})
    assert sandbox_store.query_one(properties=["a"])["a"] == 1

    sandbox_store.collection.insert_one({"a": 2, "b": 2, "sbxn": ["test"]})
    assert sandbox_store.query_one(properties=["b"], criteria={"a": 2})["b"] == 2

    sandbox_store.collection.insert_one({"a": 3, "b": 2, "sbxn": ["not_test"]})
    assert sandbox_store.query_one(properties=["c"], criteria={"a": 3}) is None


def test_sandbox_distinct(sandbox_store):
    sandbox_store.connect()
    sandbox_store.collection.insert_one({"a": 1, "b": 2, "c": 3})
    assert sandbox_store.distinct("a") == [1]

    sandbox_store.collection.insert_one({"a": 4, "d": 5, "e": 6, "sbxn": ["test"]})
    assert sandbox_store.distinct("a")[1] == 4

    sandbox_store.collection.insert_one({"a": 7, "d": 8, "e": 9, "sbxn": ["not_test"]})
    assert sandbox_store.distinct("a")[1] == 4


def test_sandbox_update(sandbox_store):
    sandbox_store.connect()
    sandbox_store.update([{"e": 6, "d": 4}], key="e")
    assert (
        next(sandbox_store.query(criteria={"d": {"$exists": 1}}, properties=["d"]))["d"]
        == 4
    )
    assert sandbox_store.collection.find_one({"e": 6})["sbxn"] == ["test"]
    sandbox_store.update([{"e": 7, "sbxn": ["core"]}], key="e")
    assert set(sandbox_store.query_one(criteria={"e": 7})["sbxn"]) == {"test", "core"}