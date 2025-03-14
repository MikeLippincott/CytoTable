"""
Utility functions for CytoTable
"""

import logging
import multiprocessing
import os
import pathlib
from typing import Union, cast

import duckdb
from cloudpathlib import AnyPath, CloudPath
from cloudpathlib.exceptions import InvalidPrefixError
from parsl.app.app import AppBase
from parsl.config import Config
from parsl.executors.threads import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# read max threads from environment if necessary
# max threads will be used with default Parsl config and Duckdb
MAX_THREADS = (
    multiprocessing.cpu_count()
    if "CYTOTABLE_MAX_THREADS" not in os.environ
    else int(cast(int, os.environ.get("CYTOTABLE_MAX_THREADS")))
)

# reference the original init
original_init = AppBase.__init__


def Parsl_AppBase_init_for_docs(self, func, *args, **kwargs):
    """
    A function to extend Parsl.app.app.AppBase with
    docstring from decorated functions rather than
    the decorators from Parsl. Used for
    Sphinx documentation purposes.
    """
    original_init(self, func, *args, **kwargs)
    # add function doc as the app doc
    self.__doc__ = func.__doc__


# set the AppBase to the new init for the docstring.
AppBase.__init__ = Parsl_AppBase_init_for_docs


def _default_parsl_config():
    """
    Return a default Parsl configuration for use with CytoTable.
    """
    return Config(
        executors=[ThreadPoolExecutor(max_threads=MAX_THREADS, label="local_threads")]
    )


# custom sort for resulting columns
def _column_sort(value: str):
    """
    A custom sort for column values as a list.
    To be used with sorted and Pyarrow tables.
    """

    # lowercase str which will be used for comparisons
    # to avoid any capitalization challenges
    value_lower = value.lower()

    # first sorted values (by list index)
    sort_first = [
        "tablenumber",
        "metadata_tablenumber",
        "imagenumber",
        "metadata_imagenumber",
        "objectnumber",
        "object_number",
    ]

    # middle sort value
    sort_middle = "metadata"

    # sorted last (by list order enumeration)
    sort_later = [
        "image",
        "cytoplasm",
        "cells",
        "nuclei",
    ]

    # if value is in the sort_first list
    # return the index from that list
    if value_lower in sort_first:
        return sort_first.index(value_lower)

    # if sort_middle is anywhere in value return
    # next index value after sort_first values
    if sort_middle in value_lower:
        return len(sort_first)

    # if any sort_later are found as the first part of value
    # return enumerated index of sort_later value (starting from
    # relative len based on the above conditionals and lists)
    if any(value_lower.startswith(val) for val in sort_later):
        for _k, _v in enumerate(sort_later, start=len(sort_first) + 1):
            if value_lower.startswith(_v):
                return _k

    # else we return the total length of all sort values
    return len(sort_first) + len(sort_later) + 1


def _duckdb_reader() -> duckdb.DuckDBPyConnection:
    """
    Creates a DuckDB connection with the
    sqlite_scanner installed and loaded.

    Returns:
        duckdb.DuckDBPyConnection
    """

    return duckdb.connect().execute(
        # note: we use an f-string here to
        # dynamically configure threads as appropriate
        f"""
        /* Install and load sqlite plugin for duckdb */
        INSTALL sqlite_scanner;
        LOAD sqlite_scanner;

        /*
        Set threads available to duckdb
        See the following for more information:
        https://duckdb.org/docs/sql/pragmas#memory_limit-threads
        */
        PRAGMA threads={MAX_THREADS};

        /*
        Allow unordered results for performance increase possibilities
        See the following for more information:
        https://duckdb.org/docs/sql/configuration#configuration-reference
        */
        PRAGMA preserve_insertion_order=FALSE;

        /*
        Allow parallel csv reads for performance increase possibilities
        See the following for more information:
        https://duckdb.org/docs/sql/configuration#configuration-reference
        */
        PRAGMA experimental_parallel_csv=TRUE;
        """,
    )


def _sqlite_mixed_type_query_to_parquet(
    source_path: str,
    table_name: str,
    chunk_size: int,
    offset: int,
    result_filepath: str,
) -> str:
    """
    Performs SQLite table data extraction where one or many
    columns include data values of potentially mismatched type
    such that the data may be exported to Arrow and a Parquet file.

    Args:
        source_path: str:
            A str which is a path to a SQLite database file.
        table_name: str:
            The name of the table being queried.
        chunk_size: int:
            Row count to use for chunked output.
        offset: int:
            The offset for chunking the data from source.
        dest_path: str:
            Path to store the output data.

    Returns:
        str:
           The resulting filepath for the table exported to parquet.
    """
    import sqlite3

    import pyarrow as pa
    import pyarrow.parquet as parquet

    # open sqlite3 connection
    with sqlite3.connect(source_path) as conn:
        cursor = conn.cursor()

        # gather table column details including datatype
        cursor.execute(
            f"""
            SELECT :table_name as table_name,
                    name as column_name,
                    type as column_type
            FROM pragma_table_info(:table_name);
            """,
            {"table_name": table_name},
        )

        # gather column metadata details as list of dictionaries
        column_info = [
            dict(zip([desc[0] for desc in cursor.description], row))
            for row in cursor.fetchall()
        ]

        # create cases for mixed-type handling in each column discovered above
        query_parts = [
            f"""
            CASE
                /* when the storage class type doesn't match the column, return nulltype */
                WHEN typeof({col['column_name']}) != '{col['column_type'].lower()}' THEN NULL
                /* else, return the normal value */
                ELSE {col['column_name']}
            END AS {col['column_name']}
            """
            for col in column_info
        ]

        # perform the select using the cases built above and using chunksize + offset
        cursor.execute(
            f'SELECT {", ".join(query_parts)} FROM {table_name} LIMIT {chunk_size} OFFSET {offset};'
        )
        # collect the results and include the column name with values
        results = [
            dict(zip([desc[0] for desc in cursor.description], row))
            for row in cursor.fetchall()
        ]

    # write results to a parquet file
    parquet.write_table(
        table=pa.Table.from_pylist(results),
        where=result_filepath,
    )

    # return filepath
    return result_filepath


def _cache_cloudpath_to_local(path: Union[str, AnyPath]) -> pathlib.Path:
    """
    Takes a cloudpath and uses cache to convert to a local copy
    for use in scenarios where remote work is not possible (sqlite).

    Args:
        path: Union[str, AnyPath]
            A filepath which will be checked and potentially
            converted to a local filepath.

    Returns:
        pathlib.Path
            A local pathlib.Path to cached version of cloudpath file.
    """

    candidate_path = AnyPath(path)

    # check that the path is a file (caching won't work with a dir)
    # and check that the file is of sqlite type
    # (other file types will be handled remotely in cloud)
    if candidate_path.is_file() and candidate_path.suffix.lower() == ".sqlite":
        try:
            # update the path to be the local filepath for reference in CytoTable ops
            # note: incurs a data read which will trigger caching of the file
            path = CloudPath(path).fspath
        except InvalidPrefixError:
            # share information about not finding a cloud path
            logger.info(
                "Did not detect a cloud path based on prefix. Defaulting to use local path operations."
            )

    # cast the result as a pathlib.Path
    return pathlib.Path(path)
