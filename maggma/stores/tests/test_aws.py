import pytest
import json
import boto3
import zlib
from moto import mock_s3
from maggma.stores import MemoryStore, AmazonS3Store
import maggma.stores.aws
from botocore.exceptions import ClientError


@pytest.fixture
def s3store():
    with mock_s3():
        conn = boto3.client("s3")
        conn.create_bucket(Bucket="bucket1")

        index = MemoryStore("index'")
        store = AmazonS3Store(index, "bucket1")
        store.connect()

        check_doc = {"task_id": "mp-1", "data": "asd"}
        store.index.update([{"task_id": "mp-1"}])
        store.s3_bucket.put_object(Key="mp-1", Body=json.dumps(check_doc).encode())

        check_doc2 = {"task_id": "mp-3", "data": "sdf"}
        store.index.update([{"task_id": "mp-3", "compression": "zlib"}])
        store.s3_bucket.put_object(
            Key="mp-3", Body=zlib.compress(json.dumps(check_doc2).encode())
        )

        yield store


def test_qeuery(s3store):
    assert s3store.query_one(criteria={"task_id": "mp-2"}) is None
    assert s3store.query_one(criteria={"task_id": "mp-1"})["data"] == "asd"
    assert s3store.query_one(criteria={"task_id": "mp-3"})["data"] == "sdf"

    assert len(list(s3store.query())) == 2


def test_update(s3store):
    s3store.update([{"task_id": "mp-2", "data": "asd"}])
    assert s3store.query_one({"task_id": "mp-2"}) is not None

    s3store.compress = True
    s3store.update([{"task_id": "mp-4", "data": "asd"}])
    assert s3store.index.query_one({"task_id": "mp-4"})["compression"] == "zlib"
    assert s3store.query_one({"task_id": "mp-4"}) is not None
    assert s3store.query_one({"task_id": "mp-4"})["data"] == "asd"


def test_remove(s3store):
    s3store.update([{"task_id": "mp-2", "data": "asd"}])
    s3store.update([{"task_id": "mp-4", "data": "asd"}])

    assert s3store.query_one({"task_id": "mp-2"}) is not None
    assert s3store.query_one({"task_id": "mp-4"}) is not None

    s3store.remove_docs({"task_id": "mp-2"})

    assert s3store.query_one({"task_id": "mp-2"}) is None
    assert s3store.query_one({"task_id": "mp-4"}) is not None


def test_close(s3store):
    list(s3store.query())
    s3store.close()
    with pytest.raises(AttributeError):
        list(s3store.query())


@pytest.fixture
def bad_import():
    temp_var = maggma.stores.aws.boto3
    maggma.stores.aws.boto3 = None
    yield
    maggma.stores.aws.boto3 = temp_var


def test_bad_impot(bad_import):
    with pytest.raises(ValueError):
        index = MemoryStore("index'")
        AmazonS3Store(index, "bucket1")


def test_aws_error(s3store):
    def raise_exception_404(data):
        error_response = {"Error": {"Code": 404}}
        raise ClientError(error_response, "raise_exception")

    def raise_exception_other(data):
        error_response = {"Error": {"Code": 405}}
        raise ClientError(error_response, "raise_exception")

    s3store.s3_bucket.Object = raise_exception_other
    with pytest.raises(ClientError):
        s3store.query_one()

    # Should just pass
    s3store.s3_bucket.Object = raise_exception_404
    s3store.query_one()
