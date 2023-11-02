import jwt
import logging
import pathlib
import numpy as np
from typing import Optional, Any, List, Dict, Union, Callable


from deeplake.util.dataset import try_flushing
from deeplake.util.path import convert_pathlib_to_string_if_needed

from deeplake.core.dataset import Dataset
from deeplake.constants import (
    DEFAULT_VECTORSTORE_TENSORS,
    MAX_BYTES_PER_MINUTE,
    TARGET_BYTE_SIZE,
    DEFAULT_VECTORSTORE_DISTANCE_METRIC,
    DEFAULT_DEEPMEMORY_DISTANCE_METRIC,
)
from deeplake.client.utils import read_token
from deeplake.client.managed.managed_client import ManagedServiceClient

from deeplake.core.vectorstore.deeplake_vectorstore import DeepLakeVectorStore
from deeplake.core.vectorstore import utils
from deeplake.core.vectorstore.vector_search import vector_search
from deeplake.core.vectorstore.vector_search import dataset as dataset_utils
from deeplake.core.vectorstore.vector_search import filter as filter_utils
from deeplake.core.vectorstore.vector_search.indra import index
from deeplake.util.bugout_reporter import (
    feature_report_path,
)
from deeplake.util.path import get_path_type


logger = logging.getLogger(__name__)


class ManagedVectorStore(DeepLakeVectorStore):
    def __init__(
        self,
        path: Union[str, pathlib.Path],
        tensor_params: List[Dict[str, object]] = None,
        embedding_function: Optional[Callable] = None,
        read_only: Optional[bool] = None,
        index_params: Optional[Dict[str, Union[int, str]]] = None,
        token: Optional[str] = None,
        overwrite: bool = False,
        verbose: bool = True,
        logger: logging.Logger = logger,
        **kwargs: Any,
    ) -> None:
        """Creates an empty ManagedVectorStore or loads an existing one if it exists at the specified ``path``.

        Examples:
            >>> # Create a vector store with default tensors
            >>> data = VectorStore(
            ...        path = "hub://org_id/dataset_name",
            ... )
            >>> # Create a vector store with custom tensors
            >>> data = VectorStore(
            ...        path = "hub://org_id/dataset_name",
            ...        tensor_params = [{"name": "text", "htype": "text"},
            ...                         {"name": "embedding_1", "htype": "embedding"},
            ...                         {"name": "embedding_2", "htype": "embedding"},
            ...                         {"name": "source", "htype": "text"},
            ...                         {"name": "metadata", "htype": "json"}
            ...                        ]
            ... )

        Args:
            path (str, pathlib.Path): - The full path for storing to the Deep Lake Vector Store. It can be:
                - a Deep Lake cloud path of the form ``hub://org_id/dataset_name``. Requires registration with Deep Lake.
            tensor_params (List[Dict[str, dict]], optional): List of dictionaries that contains information about tensors that user wants to create. See ``create_tensor`` in Deep Lake API docs for more information. Defaults to ``DEFAULT_VECTORSTORE_TENSORS``.
            embedding_function (Optional[callable], optional): Function that converts the embeddable data into embeddings. Input to `embedding_function` is a list of data and output is a list of embeddings. Defaults to None.
            read_only (bool, optional):  Opens dataset in read-only mode if True. Defaults to False.
            index_params (Dict[str, Union[int, str]]): Dictionary containing information about vector index that will be created. Defaults to None, which will utilize ``DEFAULT_VECTORSTORE_INDEX_PARAMS`` from ``deeplake.constants``. The specified key-values override the default ones.
                - threshold: The threshold for the dataset size above which an index will be created for the embedding tensor. When the threshold value is set to -1, index creation is turned off.
                             Defaults to -1, which turns off the index.
                - distance_metric: This key specifies the method of calculating the distance between vectors when creating the vector database (VDB) index. It can either be a string that corresponds to a member of the DistanceType enumeration, or the string value itself.
                    - If no value is provided, it defaults to "L2".
                    - "L2" corresponds to DistanceType.L2_NORM.
                    - "COS" corresponds to DistanceType.COSINE_SIMILARITY.
                - additional_params: Additional parameters for fine-tuning the index.
            token (str, optional): Activeloop token, used for fetching user credentials. This is Optional, tokens are normally autogenerated. Defaults to None.
            overwrite (bool): If set to True this overwrites the Vector Store if it already exists. Defaults to False.
            verbose (bool): Whether to print summary of the dataset created. Defaults to True.
            **kwargs (Any): Additional keyword arguments.

        ..
            # noqa: DAR101

        Danger:
            Setting ``overwrite`` to ``True`` will delete all of your data if the Vector Store exists! Be very careful when setting this parameter.
        """
        if embedding_function is not None:
            raise NotImplementedError(
                "ManagedVectorStore does not support embedding_function for now."
            )

        self._token = token
        self.path = convert_pathlib_to_string_if_needed(path)

        if get_path_type(self.path) != "hub":
            raise ValueError(
                "ManagedVectorStore can only be initialized with a Deep Lake Cloud path."
            )

        self.logger = logger
        self.org_id = None
        self.index_params = utils.parse_index_params(index_params)
        self.verbose = verbose
        self.tensor_params = tensor_params
        self.deep_memory = None

        self.client = ManagedServiceClient(token=self.token)

        feature_report_path(
            path,
            "vs.initialize",
            {
                "tensor_params": "default"
                if tensor_params is not None
                else tensor_params,
                "embedding_function": True if embedding_function is not None else False,
                "overwrite": overwrite,
                "read_only": read_only,
                "index_params": index_params,
                "token": self.token,
                "verbose": verbose,
                "managed": True,
            },
            token=self.token,
            username=self.username,
        )

        self.client.init_vectorstore(
            path=self.path,
            overwrite=overwrite,
            tensor_params=self.tensor_params,
        )

    @property
    def token(self):
        return self._token or read_token(from_env=True)

    @property
    def exec_option(self) -> str:
        return "tensor_db"

    @property
    def username(self) -> str:
        username = "public"
        if self.token is not None:
            username = jwt.decode(self.token, options={"verify_signature": False})["id"]

        return username

    def add(
        self,
        embedding_function: Optional[Union[Callable, List[Callable]]] = None,
        embedding_data: Optional[Union[List, List[List]]] = None,
        embedding_tensor: Optional[Union[str, List[str]]] = None,
        return_ids: bool = False,
        rate_limiter: Dict = {
            "enabled": False,
            "bytes_per_minute": MAX_BYTES_PER_MINUTE,
        },
        batch_byte_size: int = TARGET_BYTE_SIZE,
        **tensors,
    ) -> Optional[List[str]]:
        """Adding elements to deeplake vector store.

        Tensor names are specified as parameters, and data for each tensor is specified as parameter values. All data must of equal length.

        Examples:
            >>> # Dummy data
            >>> texts = ["Hello", "World"]
            >>> embeddings = [[1, 2, 3], [4, 5, 6]]
            >>> metadatas = [{"timestamp": "01:20"}, {"timestamp": "01:22"}]
            >>> emebdding_fn = lambda x: [[1, 2, 3]] * len(x)
            >>> embedding_fn_2 = lambda x: [[4, 5]] * len(x)
            >>> # Directly upload embeddings
            >>> deeplake_vector_store.add(
            ...     text = texts,
            ...     embedding = embeddings,
            ...     metadata = metadatas,
            ... )
            >>> # Upload embedding via embedding function
            >>> deeplake_vector_store.add(
            ...     text = texts,
            ...     metadata = metadatas,
            ...     embedding_function = embedding_fn,
            ...     embedding_data = texts,
            ... )
            >>> # Upload embedding via embedding function to a user-defined embedding tensor
            >>> deeplake_vector_store.add(
            ...     text = texts,
            ...     metadata = metadatas,
            ...     embedding_function = embedding_fn,
            ...     embedding_data = texts,
            ...     embedding_tensor = "embedding_1",
            ... )
            >>> # Multiple embedding functions (user defined embedding tensors must be specified)
            >>> deeplake_vector_store.add(
            ...     embedding_tensor = ["embedding_1", "embedding_2"]
            ...     embedding_function = [embedding_fn, embedding_fn_2],
            ...     embedding_data = [texts, texts],
            ... )
            >>> # Alternative syntax for multiple embedding functions
            >>> deeplake_vector_store.add(
            ...     text = texts,
            ...     metadata = metadatas,
            ...     embedding_tensor_1 = (embedding_fn, texts),
            ...     embedding_tensor_2 = (embedding_fn_2, texts),
            ... )
            >>> # Add data to fully custom tensors
            >>> deeplake_vector_store.add(
            ...     tensor_A = [1, 2],
            ...     tensor_B = ["a", "b"],
            ...     tensor_C = ["some", "data"],
            ...     embedding_function = embedding_fn,
            ...     embedding_data = texts,
            ...     embedding_tensor = "embedding_1",
            ... )

        Args:
            embedding_function (Optional[Callable]): embedding function used to convert ``embedding_data`` into embeddings. Input to `embedding_function` is a list of data and output is a list of embeddings. Overrides the ``embedding_function`` specified when initializing the Vector Store.
            embedding_data (Optional[List]): Data to be converted into embeddings using the provided ``embedding_function``. Defaults to None.
            embedding_tensor (Optional[str]): Tensor where results from the embedding function will be stored. If None, the embedding tensor is automatically inferred (when possible). Defaults to None.
            return_ids (bool): Whether to return added ids as an ouput of the method. Defaults to False.
            rate_limiter (Dict): Rate limiter configuration. Defaults to ``{"enabled": False, "bytes_per_minute": MAX_BYTES_PER_MINUTE}``.
            batch_byte_size (int): Batch size to use for parallel ingestion. Defaults to ``TARGET_BYTE_SIZE``.
            **tensors: Keyword arguments where the key is the tensor name, and the value is a list of samples that should be uploaded to that tensor.

        Returns:
            Optional[List[str]]: List of ids if ``return_ids`` is set to True. Otherwise, None.
        """

        feature_report_path(
            path=self.path,
            feature_name="vs.add",
            parameters={
                "tensors": list(tensors.keys()) if tensors else None,
                "embedding_tensor": embedding_tensor,
                "return_ids": return_ids,
                "embedding_function": True if embedding_function is not None else False,
                "embedding_data": True if embedding_data is not None else False,
                "managed": True,
            },
            token=self.token,
            username=self.username,
        )

        if embedding_function is not None or embedding_data is not None:
            raise NotImplementedError(
                "Embedding function is not supported for ManagedVectorStore. Please send precaculated embeddings."
            )

        (
            embedding_function,
            embedding_data,
            embedding_tensor,
            tensors,
        ) = utils.parse_tensors_kwargs(
            tensors, embedding_function, embedding_data, embedding_tensor
        )

        processed_tensors = {
            t: tensors[t].tolist() if isinstance(tensors[t], np.ndarray) else tensors[t]
            for t in tensors
        }
        utils.check_length_of_each_tensor(processed_tensors)

        response = self.client.vectorstore_add(
            path=self.path,
            processed_tensors=processed_tensors,
            rate_limiter=rate_limiter,
            batch_byte_size=batch_byte_size,
            return_ids=return_ids,
        )

        if return_ids:
            return response.ids

    def search(
        self,
        embedding_data: Union[str, List[str], None] = None,
        embedding_function: Optional[Callable] = None,
        embedding: Optional[Union[List[float], np.ndarray]] = None,
        k: int = 4,
        distance_metric: Optional[str] = None,
        query: Optional[str] = None,
        filter: Optional[Union[Dict, Callable]] = None,
        embedding_tensor: str = "embedding",
        return_tensors: Optional[List[str]] = None,
        return_view: bool = False,
        deep_memory: bool = False,
    ) -> Union[Dict, Dataset]:
        """VectorStore search method that combines embedding search, metadata search, and custom TQL search.

        Examples:
            >>> # Search using an embedding
            >>> data = vector_store.search(
            ...        embedding = [1, 2, 3],
            ...        exec_option = "python",
            ... )
            >>> # Search using an embedding function and data for embedding
            >>> data = vector_store.search(
            ...        embedding_data = "What does this chatbot do?",
            ...        embedding_function = query_embedding_fn,
            ...        exec_option = "compute_engine",
            ... )
            >>> # Add a filter to your search
            >>> data = vector_store.search(
            ...        embedding = np.ones(3),
            ...        exec_option = "python",
            ...        filter = {"json_tensor_name": {"key: value"}, "json_tensor_name_2": {"key_2: value_2"},...}, # Only valid for exec_option = "python"
            ... )
            >>> # Search using TQL
            >>> data = vector_store.search(
            ...        query = "select * where ..... <add TQL syntax>",
            ...        exec_option = "tensor_db", # Only valid for exec_option = "compute_engine" or "tensor_db"
            ... )

        Args:
            embedding (Union[np.ndarray, List[float]], optional): Embedding representation for performing the search. Defaults to None. The ``embedding_data`` and ``embedding`` cannot both be specified.
            embedding_data (List[str]): Data against which the search will be performed by embedding it using the `embedding_function`. Defaults to None. The `embedding_data` and `embedding` cannot both be specified.
            embedding_function (Optional[Callable], optional): function for converting `embedding_data` into embedding. Only valid if `embedding_data` is specified. Input to `embedding_function` is a list of data and output is a list of embeddings.
            k (int): Number of elements to return after running query. Defaults to 4.
            distance_metric (str): Distance metric to use for sorting the data. Avaliable options are: ``"L1", "L2", "COS", "MAX"``. Defaults to None, which uses same distance metric specified in ``index_params``. If there is no index, it performs linear search using ``DEFAULT_VECTORSTORE_DISTANCE_METRIC``.
            query (Optional[str]):  TQL Query string for direct evaluation, without application of additional filters or vector search.
            filter (Union[Dict, Callable], optional): Additional filter evaluated prior to the embedding search.

                - ``Dict`` - Key-value search on tensors of htype json, evaluated on an AND basis (a sample must satisfy all key-value filters to be True) Dict = {"tensor_name_1": {"key": value}, "tensor_name_2": {"key": value}}
                - ``Function`` - Any function that is compatible with :meth:`Dataset.filter <deeplake.core.dataset.Dataset.filter>`.

            embedding_tensor (str): Name of tensor with embeddings. Defaults to "embedding".
            return_tensors (Optional[List[str]]): List of tensors to return data for. Defaults to None, which returns data for all tensors except the embedding tensor (in order to minimize payload). To return data for all tensors, specify return_tensors = "*".
            return_view (bool): Return a Deep Lake dataset view that satisfied the search parameters, instead of a dictionary with data. Defaults to False. If ``True`` return_tensors is set to "*" beucase data is lazy-loaded and there is no cost to including all tensors in the view.
            deep_memory (bool): Whether to use the Deep Memory model for improving search results. Defaults to False if deep_memory is not specified in the Vector Store initialization.
                If True, the distance metric is set to "deepmemory_distance", which represents the metric with which the model was trained. The search is performed using the Deep Memory model. If False, the distance metric is set to "COS" or whatever distance metric user specifies.

        ..
            # noqa: DAR101

        Raises:
            ValueError: When invalid parameters are specified.
            ValueError: when deep_memory is True. Deep Memory is only available for datasets stored in the Deep Lake Managed Database for paid accounts.

        Returns:
            Dict: Dictionary where keys are tensor names and values are the results of the search
        """

        feature_report_path(
            path=self.path,
            feature_name="vs.search",
            parameters={
                "embedding_data": True if embedding_data is not None else False,
                "embedding_function": True if embedding_function is not None else False,
                "k": k,
                "distance_metric": distance_metric,
                "query": query[0:100] if query is not None else False,
                "filter": True if filter is not None else False,
                "embedding_tensor": embedding_tensor,
                "embedding": True if embedding is not None else False,
                "return_tensors": return_tensors,
                "return_view": return_view,
                "managed": True,
            },
            token=self.token,
            username=self.username,
        )

        if embedding_data is not None or embedding_function is not None:
            raise NotImplementedError(
                "ManagedVectorStore does not support embedding_function search. Please pass a precalculated embedding."
            )

        if not isinstance(filter, dict):
            raise NotImplementedError(
                "Only Filter Dictionary is supported for the ManagedVectorStore."
            )

        if return_view:
            raise NotImplementedError(
                "return_view is not supported for the ManagedVectorStore."
            )

        response = self.client.vectorstore_search(
            path=self.path,
            embedding=embedding,
            k=k,
            distance_metric=distance_metric,
            query=query,
            filter=filter,
            embedding_tensor=embedding_tensor,
            return_tensors=return_tensors,
            deep_memory=deep_memory,
        )
        return response.data

    def delete(
        self,
        row_ids: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        filter: Optional[Union[Dict, Callable]] = None,
        query: Optional[str] = None,
        delete_all: Optional[bool] = None,
    ) -> bool:
        """Delete the data in the Vector Store. Does not delete the tensor definitions. To delete the vector store completely, first run :meth:`VectorStore.delete_by_path()`.

        Examples:
            >>> # Delete using ids:
            >>> data = vector_store.delete(ids)
            >>> # Delete data using filter
            >>> data = vector_store.delete(
            ...        filter = {"json_tensor_name": {"key: value"}, "json_tensor_name_2": {"key_2: value_2"}},
            ... )
            >>> # Delete data using TQL
            >>> data = vector_store.delete(
            ...        query = "select * where ..... <add TQL syntax>",
            ...        exec_option = "compute_engine",
            ... )

        Args:
            ids (Optional[List[str]]): List of unique ids. Defaults to None.
            row_ids (Optional[List[str]]): List of absolute row indices from the dataset. Defaults to None.
            filter (Union[Dict, Callable], optional): Filter for finding samples for deletion.
                - ``Dict`` - Key-value search on tensors of htype json, evaluated on an AND basis (a sample must satisfy all key-value filters to be True) Dict = {"tensor_name_1": {"key": value}, "tensor_name_2": {"key": value}}
                - ``Function`` - Any function that is compatible with `deeplake.filter`.
            query (Optional[str]):  TQL Query string for direct evaluation for finding samples for deletion, without application of additional filters.
            exec_option (Optional[str]): Method for search execution. It could be either ``"python"``, ``"compute_engine"`` or ``"tensor_db"``. Defaults to ``None``, which inherits the option from the Vector Store initialization.
                - ``python`` - Pure-python implementation that runs on the client and can be used for data stored anywhere. WARNING: using this option with big datasets is discouraged because it can lead to memory issues.
                - ``compute_engine`` - Performant C++ implementation of the Deep Lake Compute Engine that runs on the client and can be used for any data stored in or connected to Deep Lake. It cannot be used with in-memory or local datasets.
                - ``tensor_db`` - Performant and fully-hosted Managed Tensor Database that is responsible for storage and query execution. Only available for data stored in the Deep Lake Managed Database. Store datasets in this database by specifying runtime = {"tensor_db": True} during dataset creation.
            delete_all (Optional[bool]): Whether to delete all the samples and version history of the dataset. Defaults to None.

        ..
            # noqa: DAR101

        Returns:
            bool: Returns True if deletion was successful, otherwise it raises a ValueError.

        Raises:
            ValueError: If neither ``ids``, ``filter``, ``query``, nor ``delete_all`` are specified, or if an invalid ``exec_option`` is provided.
        """

        feature_report_path(
            path=self.path,
            feature_name="vs.delete",
            parameters={
                "ids": True if ids is not None else False,
                "row_ids": True if row_ids is not None else False,
                "query": query[0:100] if query is not None else False,
                "filter": True if filter is not None else False,
                "delete_all": delete_all,
                "managed": True,
            },
            token=self.token,
            username=self.username,
        )

        if not isinstance(filter, dict):
            raise NotImplementedError(
                "Only Filter Dictionary is supported for the ManagedVectorStore."
            )

        self.client.vectorstore_remove_rows(
            path=self.path,
            indices=row_ids,
            ids=ids,
            filter=filter,
            query=query,
            delete_all=delete_all,
        )

        return True

    def update_embedding(
        self,
        embedding: Union[List[float], np.ndarray],
        row_ids: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        filter: Optional[Union[Dict, Callable]] = None,
        query: Optional[str] = None,
    ):
        """Update existing embeddings of the VectorStore, that match either query, filter, ids or row_ids.

        Examples:
            >>> # Update using ids:
            >>> data = vector_store.update(
            ...    ids,
            ...    embedding = [...]
            ... )
            >>> # Update data using filter
            >>> data = vector_store.update(
                    embedding = [...],
            ...     filter = {"json_tensor_name": {"key: value"}, "json_tensor_name_2": {"key_2: value_2"}},
            ... )
            >>> # Update data using TQL, if new embedding function is not specified the embedding_function used
            >>> # during initialization will be used
            >>> data = vector_store.update(
            ...     embedding = [...],
            ...     query = "select * where ..... <add TQL syntax>",
            ... )

        Args:
            row_ids (Optional[List[str]], optional): Row ids of the elements for replacement.
                Defaults to None.
            ids (Optional[List[str]], optional): hash ids of the elements for replacement.
                Defaults to None.
            filter (Optional[Union[Dict, Callable]], optional): Filter for finding samples for replacement.
                - ``Dict`` - Key-value search on tensors of htype json, evaluated on an AND basis (a sample must satisfy all key-value filters to be True) Dict = {"tensor_name_1": {"key": value}, "tensor_name_2": {"key": value}}
                - ``Function`` - Any function that is compatible with `deeplake.filter`
            query (Optional[str], optional): TQL Query string for direct evaluation for finding samples for deletion, without application of additional filters.
                Defaults to None.
        """
        feature_report_path(
            path=self.path,
            feature_name="vs.delete",
            parameters={
                "ids": True if ids is not None else False,
                "row_ids": True if row_ids is not None else False,
                "query": query[0:100] if query is not None else False,
                "filter": True if filter is not None else False,
                "managed": True,
            },
            token=self.token,
            username=self.username,
        )

        if not isinstance(filter, dict):
            raise NotImplementedError(
                "Only Filter Dictionary is supported for the ManagedVectorStore."
            )

        self.client.vectorstore_update_embeddings(
            path=self.path,
            embedding=embedding,
            indices=row_ids,
            ids=ids,
            filter=filter,
            query=query,
        )

    @staticmethod
    def delete_by_path(
        path: Union[str, pathlib.Path],
        token: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """Deleted the Vector Store at the specified path.

        Args:
            path (str, pathlib.Path): The full path to the Deep Lake Vector Store.
            token (str, optional): Activeloop token, used for fetching user credentials. This is optional, as tokens are normally autogenerated. Defaults to ``None``.
            force (bool): delete the path in a forced manner without rising an exception. Defaults to ``True``.

        Danger:
            This method permanently deletes all of your data if the Vector Store exists! Be very careful when using this method.
        """
        token = token or read_token(from_env=True)

        feature_report_path(
            path,
            "vs.delete_by_path",
            parameters={
                "path": path,
                "token": token,
                "force": force,
                "managed": True,
            },
            token=token,
        )

    def commit(self, allow_empty: bool = True) -> None:
        """Commits the Vector Store.

        Args:
            allow_empty (bool): Whether to allow empty commits. Defaults to True.
        """
        raise NotImplementedError

    def checkout(self, branch: str = "main") -> None:
        """Checkout the Vector Store to a specific branch.

        Args:
            branch (str): Branch name to checkout. Defaults to "main".
        """
        raise NotImplementedError

    def _get_summary(self):
        """Returns a summary of the Managed Vector Store."""
        return self.client.get_vectorstore_summary(self.path)

    def tensors(self):
        """Returns the list of tensors present in the dataset"""
        return [t["name"] for t in self._get_summary().tensors]

    def summary(self):
        """Prints a summary of the dataset"""
        print(self._get_summary().summary)

    def __len__(self):
        """Length of the dataset"""
        return self._get_summary().length
