"""
Example builders for testing and general use.
"""
import traceback
from abc import ABCMeta, abstractmethod
from datetime import datetime

from maggma.builder import Builder
from maggma.utils import confirm_field_index, grouper


def source_keys_updated(source, target, query=None):
    """
    Utility for incremental building. Gets a list of source.key values.

    Get key values for source documents that have been updated with respect to
    corresponding target documents.

    Called should ensure [(lu_field, -1),(key, 1)] compound index on both source
    and target.

    """
    keys_updated = set()  # Handle non-unique keys, e.g. for GroupBuilder.
    cursor_source = source.query(
        criteria=query, properties=[source.key, source.lu_field], sort=[(source.lu_field, -1), (source.key, 1)])
    cursor_target = target.query(
        properties=[target.key, target.lu_field], sort=[(target.lu_field, -1), (target.key, 1)])
    tdoc = next(cursor_target, None)
    for sdoc in cursor_source:
        if tdoc is None:
            keys_updated.add(sdoc[source.key])
        elif tdoc[target.key] == sdoc[source.key]:
            if target.lu_func[0](tdoc[target.lu_field]) < source.lu_func[0](sdoc[source.lu_field]):
                keys_updated.add(sdoc[source.key])
            tdoc = next(cursor_target, None)
        else:
            keys_updated.add(sdoc[source.key])
    return list(keys_updated)


def get_keys(source, target, query=None, incremental=True, logger=None):
    """Return keys to pass to `source.query` to get items."""
    index_checks = [confirm_field_index(target, target.key)]
    if incremental:
        # Ensure [(lu_field, -1), (key, 1)] index on both source and target
        for store in (source, target):
            info = store.collection.index_information().values()
            index_checks.append(
                any(spec == [(store.lu_field, -1), (store.key, 1)] for spec in (index['key'] for index in info)))
    if not all(index_checks):
        index_warning = ("Missing one or more important indices on stores. "
                         "Performance for large stores may be severely degraded. "
                         "Ensure indices on target.key and "
                         "[(store.lu_field, -1), (store.key, 1)] "
                         "for each of source and target.")
        if logger:
            logger.warning(index_warning)

    keys_updated = source_keys_updated(source, target, query)

    return keys_updated


class MapBuilder(Builder, metaclass=ABCMeta):
    """
    Apply a unary function to yield a target document for each source document.

    Supports incremental building, where a source document gets built only if it
    has newer (by lu_field) data than the corresponding (by key) target
    document.

    """

    def __init__(self, source, target, ufn, query=None, incremental=True, projection=None, **kwargs):
        """
        Apply a unary function to each source document.

        Args:
            source (Store): source store
            target (Store): target store
            ufn (function): Unary function to process item
                            You do not need to provide values for
                            source.key and source.lu_field in the output.
                            Any uncaught exceptions will be caught by 
                            process_item and logged to the "error" field 
                            in the target document.
            query (dict): optional query to filter source store
            incremental (bool): whether to use lu_field of source and target
                to get only new/updated documents.
            projection (list): list of keys to project from the source for processing.
                This can be used to limit the data to improve efficiency
        """
        self.source = source
        self.target = target
        self.incremental = incremental
        self.query = query
        self.ufn = ufn
        self.projection = projection if projection else []
        self.kwargs = kwargs
        super().__init__(sources=[source], targets=[target], **kwargs)

    def get_items(self):

        self.logger.info("Starting {} Builder".format(self.__class__.__name__))
        keys = get_keys(source=self.source, target=self.target, query=self.query, logger=self.logger)

        self.logger.info("Processing {} items".format(len(keys)))

        if self.projection:
            projection = list(set(self.projection + [self.source.key, self.source.lu_field]))
        else:
            projection = None

        self.total = len(keys)
        for chunked_keys in grouper(keys, self.chunk_size, None):
            chunked_keys = list(filter(None.__ne__, chunked_keys))
            for doc in self.source.query(criteria={self.source.key: {"$in": chunked_keys}}, properties=projection):
                yield doc

    def process_item(self, item):

        self.logger.debug("Processing: {}".format(item[self.source.key]))

        try:
            processed = self.ufn.__call__(item)
        except Exception as e:
            self.logger.error(traceback.format_exc())
            processed = {"error": str(e)}
        key, lu_field = self.source.key, self.source.lu_field
        out = {self.target.key: item[key]}
        out[self.target.lu_field] = self.source.lu_func[0](item[self.source.lu_field])
        out.update(processed)
        return out

    def update_targets(self, items):
        source, target = self.source, self.target
        for item in items:
            # Use source last-updated value, ensuring `datetime` type.
            item[target.lu_field] = source.lu_func[0](item[source.lu_field])
            if source.lu_field != target.lu_field:
                del item[source.lu_field]
            item["_bt"] = datetime.utcnow()
            if "_id" in item:
                del item["_id"]

        if len(items) > 0:
            target.update(items, update_lu=False)


class GroupBuilder(MapBuilder, metaclass=ABCMeta):
    """
    Group source docs and produce one target doc from each group.

    Supports incremental building, where a source group gets (re)built only if
    it has a newer (by lu_field) doc than the corresponding (by key) target doc.
    """

    def __init__(self, source, target, query=None, incremental=True, **kwargs):
        """

        Given criteria, get docs with needed grouping properties. With these
        minimal docs, yield groups. For each group, fetch all needed data for
        item processing, and yield one or more items (i.e. subgroups as
        appropriate).

        Args:
            source (Store): source store
            target (Store): target store
            query (dict): optional query to filter source store
            incremental (bool): whether to use lu_field of source and target
                to get only new/updated documents.
        """
        super().__init__(source, target, query=query, incremental=incremental, **kwargs)
        self.total = None

    def get_items(self):
        criteria = get_keys(
            self.source, self.target, query=self.query, incremental=self.incremental, logger=self.logger)
        if all(isinstance(entry, str) for entry in self.grouping_properties()):
            properties = {entry: 1 for entry in self.grouping_properties()}
            if "_id" not in properties:
                properties.update({"_id": 0})
        else:
            properties = {entry: include for entry, include in self.grouping_properties()}
        groups = self.docs_to_groups(self.source.query(criteria=criteria, properties=properties))
        self.total = len(groups)
        if hasattr(self, "n_items_per_group"):
            n = self.n_items_per_group
            if isinstance(n, int) and n >= 1:
                self.total *= n
        for group in groups:
            for item in self.group_to_items(group):
                yield item

    @staticmethod
    @abstractmethod
    def grouping_properties():
        """
        Needed projection for docs_to_groups (passed to source.query).

        Returns:
            list or dict: of the same form as projection param passed to
                pymongo.collection.Collection.find. If a list, it is converted
                to dict form with {"_id": 0} unless "_id" is explicitly
                included in the list. This is to ease use of index-covered
                queries in docs_to_groups.
        """

    @staticmethod
    @abstractmethod
    def docs_to_groups(docs):
        """
        Yield groups from (minimally-projected) documents.

        This could be as simple as returning a set of unique document keys.

        Args:
            docs (pymongo.cursor.Cursor): documents with minimal projections
                needed to determine groups.

        Returns:
            iterable: one group at a time
        """

    @abstractmethod
    def group_to_items(self, group):
        """
        Given a group, yield items for this builder's process_item method.

        This method does the work of fetching data needed for processing.

        Args:
            group (dict): sufficient as or to produce a source filter

        Returns:
            iterable: one or more items per group for process_item.
        """


class CopyBuilder(MapBuilder):
    """Sync a source store with a target store."""

    def __init__(self, source, target, **kwargs):
        super().__init__(source=source, target=target, ufn=lambda x: x, **kwargs)
