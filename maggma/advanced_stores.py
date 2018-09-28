# coding: utf-8
"""
Advanced Stores for behavior outside normal access patterns
"""
import os
import hvac
import json
import zlib
from datetime import datetime

from maggma.stores import Store, MongoStore
from maggma.utils import lazy_substitute, substitute
from pydash import get, set_
from monty.json import jsanitize
from monty.functools import lru_cache
from pymongo import MongoClient

try:
    import boto3
    import botocore
    boto_import = True
except:
    boto_import = False


class VaultStore(MongoStore):
    """
    Extends MongoStore to read credentials out of Vault server
    and uses these values to initialize MongoStore instance
    """

    def __init__(self, collection_name, vault_secret_path):
        """
        collection (string): name of mongo collection
        vault_secret_path (string): path on vault server with mongo creds object

        Environment (must be set prior to invocation):
        VAULT_ADDR - URL of vault server (eg. https://matgen8.lbl.gov:8200)
        VAULT_TOKEN or GITHUB_TOKEN - token used to authenticate to vault
        """
        vault_addr = os.getenv("VAULT_ADDR")

        if not vault_addr:
            raise RuntimeError("VAULT_ADDR not set")

        client = hvac.Client(vault_addr)

        # If we have a vault token use this
        token = os.getenv("VAULT_TOKEN")

        # Look for a github token instead
        if not token:
            github_token = os.getenv("GITHUB_TOKEN")

            if github_token:
                client.auth_github(github_token)
            else:
                raise RuntimeError("VAULT_TOKEN or GITHUB_TOKEN not set")
        else:
            client.token = token
            if not client.is_authenticated():
                raise RuntimeError("Bad token")

        # Read the vault secret
        json_db_creds = client.read(vault_secret_path)
        db_creds = json.loads(json_db_creds['data']['value'])

        database = db_creds.get("db")
        host = db_creds.get("host", "localhost")
        port = db_creds.get("port", 27017)
        username = db_creds.get("username", "")
        password = db_creds.get("password", "")

        super(VaultStore, self).__init__(database, collection_name, host, port, username, password)


class AliasingStore(Store):
    """
    Special Store that aliases for the primary accessors
    """

    def __init__(self, store, aliases, **kwargs):
        """
        store (Store): the store to wrap around
        aliases (dict): dict of aliases of the form external key: internal key
        """
        self.store = store
        # Given an external key tells what the internal key is
        self.aliases = aliases
        # Given the internal key tells us what the external key is
        self.reverse_aliases = {v: k for k, v in aliases.items()}
        self.kwargs = kwargs

        kwargs.update({"lu_field": store.lu_field, "lu_type": store.lu_type})
        super(AliasingStore, self).__init__(**kwargs)

    def query(self, properties=None, criteria=None, **kwargs):

        if isinstance(properties, list):
            properties = {p: 1 for p in properties}

        criteria = criteria if criteria else {}
        substitute(properties, self.reverse_aliases)
        lazy_substitute(criteria, self.reverse_aliases)
        for d in self.store.query(properties, criteria, **kwargs):
            substitute(d, self.aliases)
            yield d

    def query_one(self, properties=None, criteria=None, **kwargs):

        if isinstance(properties, list):
            properties = {p: 1 for p in properties}

        criteria = criteria if criteria else {}
        substitute(properties, self.reverse_aliases)
        lazy_substitute(criteria, self.reverse_aliases)
        d = self.store.query_one(properties, criteria, **kwargs)
        substitute(d, self.aliases)
        return d

    def distinct(self, key, criteria=None, all_exist=True, **kwargs):
        if isinstance(key, list):
            criteria = criteria if criteria else {}
            # Update to ensure keys are there
            if all_exist:
                criteria.update({k: {"$exists": True} for k in key if k not in criteria})

            results = []
            for d in self.groupby(key, properties=key, criteria=criteria):
                results.append(d["_id"])
            return results

        else:
            criteria = criteria if criteria else {}
            lazy_substitute(criteria, self.reverse_aliases)
            key = self.aliases[key] if key in self.aliases else key
            return self.collection.distinct(key, filter=criteria, **kwargs)

    def groupby(self, keys, properties=None, criteria=None, **kwargs):
        # Convert to a list
        keys = keys if isinstance(keys, list) else [keys]

        # Make the aliasing transformations on keys
        keys = [self.aliases[k] if k in self.aliases else k for k in keys]

        # Update criteria and properties based on aliases
        criteria = criteria if criteria else {}
        substitute(properties, self.reverse_aliases)
        lazy_substitute(criteria, self.reverse_aliases)

        return self.store.groupby(keys=keys, properties=properties, criteria=criteria, **kwargs)

    def update(self, docs, update_lu=True, key=None):
        key = key if key else self.key

        for d in docs:
            substitute(d, self.reverse_aliases)

        if key in self.aliases:
            key = self.aliases[key]

        self.store.update(docs, update_lu=update_lu, key=key)

    def ensure_index(self, key, unique=False, **kwargs):
        if key in self.aliases:
            key = self.aliases
        return self.store.ensure_index(key, unique, **kwargs)

    def close(self):
        self.store.close()

    @property
    def collection(self):
        return self.store.collection

    def connect(self, force_reset=False):
        self.store.connect(force_reset=force_reset)


class SandboxStore(Store):
    """
    Provides a sandboxed view to another store
    """

    def __init__(self, store, sandbox, exclusive=False):
        """
        store (Store): store to wrap sandboxing around
        sandbox (string): the corresponding sandbox
        exclusive (bool): whether to be exclusively in this sandbox or include global items
        """
        self.store = store
        self.sandbox = sandbox
        self.exclusive = exclusive
        super().__init__(key=self.store.key,
                         lu_field=self.store.lu_field,
                         lu_type=self.store.lu_type,
                         validator=self.store.validator)

    @property
    @lru_cache(maxsize=1)
    def sbx_criteria(self):
        if self.exclusive:
            return {"sbxn": self.sandbox}
        else:
            return {"$or": [{"sbxn": {"$in": [self.sandbox]}},
                            {"sbxn": {"$exists": False}}]}

    def query(self, properties=None, criteria=None, **kwargs):
        criteria = dict(**criteria, **self.sbx_criteria) if criteria else self.sbx_criteria
        return self.store.query(properties=properties, criteria=criteria, **kwargs)

    def query_one(self, properties=None, criteria=None, **kwargs):
        criteria = dict(**criteria, **self.sbx_criteria) if criteria else self.sbx_criteria
        return self.store.query_one(properties=properties, criteria=criteria, **kwargs)

    def distinct(self, key, criteria=None, **kwargs):
        criteria = dict(**criteria, **self.sbx_criteria) if criteria else self.sbx_criteria
        return self.store.distinct(key=key, criteria=criteria, **kwargs)

    def groupby(self, keys, properties=None, criteria=None, **kwargs):
        criteria = dict(**criteria, **self.sbx_criteria) if criteria else self.sbx_criteria

        return self.store.groupby(keys=keys, properties=properties, criteria=criteria, **kwargs)

    def update(self, docs, update_lu=True, key=None):
        for d in docs:
            if "sbxn" in d:
                d["sbxn"] = list(set(d["sbxn"] + [self.sandbox]))
            else:
                d["sbxn"] = [self.sandbox]

        self.store.update(docs, update_lu=update_lu, key=key)

    def ensure_index(self, key, unique=False, **kwargs):
        return self.store.ensure_index(key, unique, **kwargs)

    def close(self):
        self.store.close()

    @property
    def collection(self):
        return self.store.collection

    def connect(self, force_reset=False):
        self.store.connect(force_reset=force_reset)


class AmazonS3Store(Store):
    """
    GridFS like storage using Amazon S3 and a regular store for indexing
    Assumes Amazon AWS key and secret key are set in environment or default config file
    """

    def __init__(self, index, bucket, **kwargs):
        """
        Initializes an S3 Store
        Args:
            index (Store): a store to use to index the S3 Bucket
            bucket (str) : name of the bucket
        """
        if not boto_import:
            raise ValueError("boto not available, please install boto3 to "
                             "use AmazonS3Store")
        self.index = index
        self.bucket = bucket
        self.s3 = None
        self.s3_bucket = None
        # Force the key to be the same as the index
        kwargs["key"] = index.key
        super(AmazonS3Store, self).__init__(**kwargs)

    def connect(self, force_reset=False):
        self.index.connect(force_reset=force_reset)
        if not self.s3:
            self.s3 = boto3.resource("s3")
            # TODO: Provide configuration variable to create bucket if not present
            if self.bucket not in self.s3.list_buckets():
                raise Exception("Bucket not present on AWS: {}".format(self.bucket))
            self.s3_bucket = self.s3.Bucket(self.bucket)

    def close(self):
        self.index.close()

    @property
    def collection(self):
        # For now returns the index collection since that is what we would "search" on
        return self.index

    def query(self, properties=None, criteria=None, **kwargs):
        """
        Function that gets data from Amazon S3. This store ignores all
        property projections as its designed for whole document access

        Args:
            properties (list or dict): This will be ignored by the S3
                Store
            criteria (dict): filter for query, matches documents
                against key-value pairs
            **kwargs (kwargs): further kwargs to Collection.find
        """
        for f in self.index.query(criteria=criteria, **kwargs):
            try:
                data = self.s3_bucket.Object(f[self.key]).get()
            except botocore.exceptions.ClientError as e:
                # If a client error is thrown, then check that it was a 404 error.
                # If it was a 404 error, then the object does not exist.
                error_code = int(e.response['Error']['Code'])
                if error_code == 404:
                    self.logger.error("Could not find S3 object {}".format(f[self.key]))
                    break

            if f.get("compression", "") is "zlib":
                data = zlib.decompress(data)

            yield json.loads(data)

    def query_one(self, properties=None, criteria=None, **kwargs):
        """
        Function that gets a single document from Amazon S3. This store
        ignores all property projections as its designed for whole
        document access

        Args:
            properties (list or dict): This will be ignored by the S3
                Store
            criteria (dict): filter for query, matches documents
                against key-value pairs
            **kwargs (kwargs): further kwargs to Collection.find
        """
        f = self.index.query_one(criteria=criteria, **kwargs)
        if f:
            try:
                data = self.s3_bucket.Object(f[self.key]).get()
            except botocore.exceptions.ClientError as e:
                # If a client error is thrown, then check that it was a 404 error.
                # If it was a 404 error, then the object does not exist.
                error_code = int(e.response['Error']['Code'])
                if error_code == 404:
                    self.logger.error("Could not find S3 object {}".format(f[self.key]))
                    return None

            if f.get("compression", "") is "zlib":
                data = zlib.decompress(data)

            return json.loads(data)
        else:
            return None

    def distinct(self, key, criteria=None, all_exist=False, **kwargs):
        """
        Function get to get all distinct values of a certain key in the
        AmazonS3 Store. This searches the index collection for this data

        Args:
            key (mongolike key or list of mongolike keys): key or keys
                for which to find distinct values or sets of values.
            criteria (filter criteria): criteria for filter
            all_exist (bool): whether to ensure all keys in list exist
                in each document, defaults to False
            **kwargs (kwargs): kwargs corresponding to collection.distinct
        """
        # Index is a store so it should have its own distinct function
        return self.index.distinct(key, filter=criteria, **kwargs)

    def groupby(self, keys, properties=None, criteria=None, **kwargs):
        """
        Simple grouping function that will group documents
        by keys. Only searches the index collection

        Args:
            keys (list or string): fields to group documents
            properties (list): properties to return in grouped documents
            criteria (dict): filter for documents to group
            allow_disk_use (bool): whether to allow disk use in aggregation

        Returns:
            command cursor corresponding to grouped documents

            elements of the command cursor have the structure:
            {'_id': {"KEY_1": value_1, "KEY_2": value_2 ...,
             'docs': [list_of_documents corresponding to key values]}

        """
        self.index.groupby(keys, properties, criteria, **kwargs)

    def ensure_index(self, key, unique=False):
        """
        Wrapper for pymongo.Collection.ensure_index for the files collection
        """
        return self.index.ensure_index(key, unique=unique, background=True)

    def update(self, docs, update_lu=True, key=None, compress=False):
        """
        Function to update associated MongoStore collection.

        Args:
            docs ([dict]): list of documents
            key ([str] or str): keys to use to build search doc
            compress (bool): compress the document or not
        """
        now = datetime.now()
        search_docs = []
        for d in docs:
            if isinstance(key, list):
                search_doc = {k: d[k] for k in key}
            elif key:
                search_doc = {key: d[key]}
            else:
                search_doc = {}

            # Always include our main key
            search_doc[self.key] = d[self.key]

            # Remove MongoDB _id from search
            if "_id" in search_doc:
                del search_doc["_id"]

            # Add a timestamp
            if update_lu:
                search_doc[self.lu_field] = now
                d[self.lu_field] = now

            data = json.dumps(jsanitize(d)).encode()

            # Compress with zlib if chosen
            if compress:
                search_doc["compression"] = "zlib"
                data = zlib.compress(data)

            self.s3_bucket.put_object(Key=d[self.key], Body=data, Metadata=search_doc)
            search_docs.append(search_doc)

        # Use store's update to remove key clashes
        self.index.update(search_docs)

    @property
    def last_updated(self):
        return self.index.last_updated

    def lu_filter(self, targets):
        """Creates a MongoDB filter for new documents.

        By "new", we mean documents in this Store that were last updated later
        than any document in targets.

        Args:
            targets (list): A list of Stores

        """
        self.index.lu_filter(targets)

    def __hash__(self):
        return hash((self.index.__hash__, self.bucket))

    def rebuild_index_from_s3_data(self):
        """
        Rebuilds the index Store from the data in S3
        Relies on the index document being stores as the metadata for the file
        """
        index_docs = []
        for file in self.s3_bucket.objects.all():
            # TODO: Transform the data back from strings and remove AWS S3 specific keys
            index_docs.append(file.metadata)

        self.index.update(index_docs)


class JointStore(Store):
    """Store corresponding to multiple collections, uses lookup to join"""
    def __init__(self, database, collection_names, host="localhost",
                 port=27017, username="", password="", master=None, **kwargs):
        self.database = database
        self.collection_names = collection_names
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._collection = None
        self.master = master or collection_names[0]
        self.kwargs = kwargs
        super(JointStore, self).__init__(**kwargs)

    def connect(self, force_reset=False):
        conn = MongoClient(self.host, self.port)
        db = conn[self.database]
        if self.username is not "":
            db.authenticate(self.username, self.password)
        self._collection = db[self.master]

    def close(self):
        self.collection.database.client.close()

    @property
    def collection(self):
        return self._collection

    @property
    def nonmaster_names(self):
        return list(set(self.collection_names) - {self.master})

    def query(self, criteria=None, properties=None, **kwargs):
        pipeline = self._get_pipeline(properties, criteria)
        return self.collection.aggregate(pipeline, **kwargs)

    @property
    def last_updated(self):
        lus = []
        for cname in self.collection_names:
            lu = MongoStore.from_collection(
                self.collection.database[cname],
                lu_field=self.lu_field).last_updated
            lus.append(lu)
        return max(lus)

    # TODO: implement update?
    def update(self, docs, update_lu=True, key=None, **kwargs):
        raise NotImplementedError("No update method for JointStore")

    def _get_store_by_name(self, name):
        return MongoStore.from_collection(self.collection.database[name])

    def distinct(self, key, criteria=None, all_exist=True, **kwargs):
        g_key = key if isinstance(key, list) else [key]
        if all_exist:
            criteria = criteria or {}
            criteria.update({k: {"$exists": True} for k in g_key
                             if k not in criteria})
        cursor = self.groupby(g_key, criteria=criteria, **kwargs)
        if isinstance(key, list):
            return [d['_id'] for d in cursor]
        else:
            return [get(d['_id'], key) for d in cursor]

    def ensure_index(self):
        raise NotImplementedError("No update method for JointStore")

    def _get_pipeline(self, criteria=None, properties=None):
        """
        Gets the aggregation pipeline for query and query_one

        Args:
            properties: properties to be returned
            criteria: criteria to filter by

        Returns:
            list of aggregation operators
        """
        pipeline = []
        for cname in self.collection_names:
            if cname is not self.master:
                pipeline.append({
                    "$lookup": {"from": cname, "localField": self.key,
                                "foreignField": self.key, "as": cname}})
                pipeline.append({
                    "$unwind": {"path": "${}".format(cname),
                                "preserveNullAndEmptyArrays": True}})

        # Do projection for max last_updated
        lu_proj = {self.lu_field: {"$max": ["${}.{}".format(cname, self.lu_field)
                                            for cname in self.collection_names]}}
        pipeline.append({"$addFields": lu_proj})

        if criteria:
            pipeline.append({"$match": criteria})
        if isinstance(properties, list):
            properties = {k: 1 for k in properties}
        if properties:
            pipeline.append({"$project": properties})
        return pipeline

    def groupby(self, keys, criteria=None, properties=None, **kwargs):
        pipeline = self._get_pipeline(criteria=criteria, properties=properties)
        if not isinstance(keys, list):
            keys = [keys]
        group_id = {}
        for key in keys:
            set_(group_id, key, "${}".format(key))
        pipeline.append({"$group": {"_id": group_id,
                                    "docs": {"$push": "$$ROOT"}}})

        return self.collection.aggregate(pipeline, **kwargs)

    def query_one(self, properties=None, criteria=None, **kwargs):
        """
        Get one document

        Args:
            properties([str] or {}): properties to return in query
            criteria ({}): filter for matching
            **kwargs: kwargs for collection.aggregate

        Returns:
            single document
        """
        # TODO: maybe adding explicit limit in agg pipeline is better as below?
        # pipeline = self._get_pipeline(properties, criteria)
        # pipeline.append({"$limit": 1})
        query = self.query(properties, criteria, **kwargs)
        try:
            doc = query.next()
            return doc
        except StopIteration:
            return None
