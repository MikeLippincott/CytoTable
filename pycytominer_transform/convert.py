"""
pycytominer-transform: convert - transforming data for use with pyctyominer.
"""

import pathlib
from typing import Any, Dict, List, Literal, Optional

import pyarrow as pa
from prefect import flow, task
from prefect.futures import PrefectFuture
from prefect.task_runners import BaseTaskRunner, ConcurrentTaskRunner
from pyarrow import csv, parquet

DEFAULT_TARGETS = ["image", "cells", "nuclei", "cytoplasm"]


@task
def get_source_filepaths(
    path: str, targets: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Gather dataset of filepaths from a provided directory path.

    Args:
      path: str:
        Path to seek filepaths within.
      targets: List[str]:
        Target filenames to seek within the provided path.

    Returns:
      Dict[str, List[Dict[str, Any]]]
        Data structure which groups related files based on the targets.
    """

    records = []

    # gathers files from provided path using targets as a filter
    for file in pathlib.Path(path).glob("**/*"):
        if file.is_file() and (str(file.stem).lower() in targets or targets is None):
            records.append({"source_path": file})

    # if we collected no files above, raise exception
    if len(records) < 1:
        raise Exception(
            f"No input data to process at path: {str(pathlib.Path(path).resolve())}"
        )

    grouped_records = {}

    # group files together by similar filename for potential concatenation later
    for unique_source in set(source["source_path"].name for source in records):
        grouped_records[unique_source] = [
            source for source in records if source["source_path"].name == unique_source
        ]

    return grouped_records


@task
def read_csv(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Read csv file from record.

    Args:
      record: Dict[str, Any]:
        Data containing filepath to csv file

    Returns:
      Dict[str, Any]
        Updated dictionary with CSV data in-memory
    """

    # read csv using pyarrow lib
    table = csv.read_csv(input_file=record["source_path"])

    # attach table data to record
    record["table"] = table

    return record


@flow
def concat_records(
    records: Dict[str, List[Dict[str, Any]]], dest_path: Optional[str] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Concatenate similar tables together as unified dataset.

    Args:
      records: Dict[str, List[Dict[str, Any]]]:
        Data structure containing potentially grouped data for concatenation.
      dest_path: Optional[str] (Default value = None)
        Optional destination path for concatenated records.

    Returns:
      Dict[str, List[Dict[str, Any]]]
        Updated dictionary containing concatted records (where they existed)
    """

    for group in records:
        # if we have less than 2 records, no need to concat
        if len(records[group]) < 2:
            continue

        # build a new record group
        records[group] = concat_record_group.submit(
            record_group=records[group], dest_path=dest_path
        )

    # wait for futures processing from submit to complete
    results = {
        key: value.result() if isinstance(value, PrefectFuture) else value
        for key, value in records.items()
    }

    return results


@task
def concat_record_group(
    record_group: List[Dict[str, Any]], dest_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Concatenate group of records together as unified dataset.

    Args:
      records: List[Dict[str, Any]]:
        Data structure containing grouped data for concatenation.
      dest_path: Optional[str] (Default value = None)
        Optional destination path for concatenated records.

    Returns:
      List[Dict[str, Any]]
        Updated dictionary containing concatted records
    """

    concatted = [
        {
            # source path becomes parent's parent dir with the same filename
            "source_path": pathlib.Path(
                (
                    f"{record_group[0]['source_path'].parent.parent}"
                    f"/{record_group[0]['source_path'].stem}"
                )
            )
        }
    ]

    # for concatting arrow tables
    if "table" in record_group[0]:
        # table becomes the result of concatted tables
        concatted[0]["table"] = pa.concat_tables(
            [record["table"] for record in record_group]
        )

    # for concatting parquet files
    elif "destination_path" in record_group[0]:
        # make the new source path dir if it doesn't already exist

        destination_path = pathlib.Path(
            (
                f"{dest_path}"
                f"/{record_group[0]['source_path'].parent.parent.name}"
                f".{record_group[0]['source_path'].stem}"
                ".parquet"
            )
        )

        # if there's already a file remove it
        if destination_path.exists():
            destination_path.unlink()

        # read first file for basis of schema and column order for all others
        writer_basis = parquet.read_table(record_group[0]["destination_path"])

        # build a parquet file writer which will be used to append files
        # as a single concatted parquet file, referencing the first file's schema
        # (all must be the same schema)
        writer = parquet.ParquetWriter(str(destination_path), writer_basis.schema)

        for table in [record["destination_path"] for record in record_group]:
            # read the file from the list and write to the concatted parquet file
            # note: we pass column order based on the first chunk file to help ensure schema
            # compatibility for the writer
            writer.write_table(
                parquet.read_table(table, columns=writer_basis.column_names)
            )
            # remove the file which was written in the concatted parquet file (we no longer need it)
            pathlib.Path(table).unlink()

        # close the single concatted parquet file writer
        writer.close()

        # return the concatted parquet filename
        concatted[0]["destination_path"] = destination_path

    return concatted


@task
def write_parquet(
    record: Dict[str, Any], dest_path: str, unique_name: bool = False
) -> Dict[str, Any]:
    """
    Write parquet data using in-memory data.

    Args:
      record: Dict:
        Dictionary including in-memory data which will be written to parquet.
      dest_path: str:
        Destination path to write the parquet file to.
      unique_name: bool:  (Default value = False)
        Determines whether a unique name is necessary for the file.

    Returns:
      Dict[str, Any]
        Updated dictionary containing the destination path where parquet file
        was written.
    """

    # make the dest_path dir if it doesn't already exist
    pathlib.Path(dest_path).mkdir(parents=True, exist_ok=True)

    # build a default destination path for the parquet output
    destination_path = pathlib.Path(
        f"{dest_path}/{str(record['source_path'].stem)}.parquet"
    )

    # build unique names to avoid overlaps
    if unique_name:
        destination_path = pathlib.Path(
            (
                f"{dest_path}/{str(record['source_path'].parent.name)}"
                f".{str(record['source_path'].stem)}.parquet"
            )
        )

    # write the table to destination path output
    parquet.write_table(table=record["table"], where=destination_path)

    # unset table
    del record["table"]

    # update the record to include the destination path
    record["destination_path"] = destination_path

    return record


@task
def infer_source_datatype(
    records: Dict[str, List[Dict[str, Any]]], target_datatype: Optional[str] = None
) -> str:
    """
    Infers and optionally validates datatype (extension) of files.

    Args:
      records: Dict[str, List[Dict[str, Any]]]:
        Grouped datasets of files which will be used by other functions.
      target_datatype: Optional[str]:  (Default value = None)
        Optional target datatype to validate within the context of
        detected datatypes.

    Returns:
      str
        A string of the datatype detected or validated target_datatype.
    """

    # gather file extension suffixes
    suffixes = list(set((group.split(".")[-1]).lower() for group in records))

    # if we don't have a target datatype and have more than one suffix
    # we can't infer which file type to read.
    if target_datatype is None and len(suffixes) > 1:
        raise Exception(
            f"Detected more than one inferred datatypes from source path: {suffixes}"
        )

    # if we have a target datatype and the target isn't within the detected suffixes
    # we will have no files to process.
    if target_datatype is not None and target_datatype not in suffixes:
        raise Exception(
            (
                f"Unable to find targeted datatype {target_datatype} "
                "within files. Detected datatypes: {suffixes}"
            )
        )

    # if we haven't set a target datatype and need to rely on the inferred one
    # set it so it may be returned
    if target_datatype is None:
        target_datatype = suffixes[0]

    return target_datatype


@task
def filter_source_filepaths(
    records: Dict[str, List[Dict[str, Any]]], target_datatype: str
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Filter source filepaths based on provided target_datatype

    Args:
      records: Dict[str, List[Dict[str, Any]]]
        Grouped datasets of files which will be used by other functions.
      target_datatype: str
        Target datatype to use for filtering the dataset.

    Returns:
      Dict[str, List[Dict[str, Any]]]
        Data structure which groups related files based on the targets.
    """

    return {
        key: val
        for key, val in records.items()
        if pathlib.Path(key).suffix == f".{target_datatype}"
    }


@flow
def gather_records(
    path: str,
    source_datatype: Optional[str] = None,
    targets: Optional[List[str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:

    """
    Flow for gathering records for conversion

    Args:
      path: str:
        Where to gather file-based data from.
      source_datatype: Optional[str]:  (Default value = None)
        The source datatype (extension) to use for reading the tables.
      targets: Optional[List[str]]:  (Default value = None)
        The source file names to target within the provided path.

    Returns:
      Dict[str, List[Dict[str, Any]]]
        Data structure which groups related files based on the targets.
    """

    # if we have no targets, set the defaults
    if targets is None:
        targets = DEFAULT_TARGETS

    # gather filepaths which will be used as the basis for this work
    records = get_source_filepaths(path=path, targets=targets)

    # infer or validate the source datatype based on source filepaths
    source_datatype = infer_source_datatype(
        records=records, target_datatype=source_datatype
    )

    # filter source filepaths to inferred or targeted datatype
    records = filter_source_filepaths(records=records, target_datatype=source_datatype)

    return records


@flow
def read_files(
    record_group_name: str, record_group: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Read files based on their suffix (extension)

    Args:
      record_group_name: str
        Name of the file including extension for the group
      record_group: List[Dict[str, Any]]
        List of dictionaries containing data related to the files

    Returns:
      List[Dict[str, Any]]
        Updated list of dictionaries containing data related to the files
    """

    if pathlib.Path(record_group_name).suffix == ".csv":
        tables_map = read_csv.map(record=record_group)

    # recollect the group of mapped read records
    return [table.result() for table in tables_map]


@flow
def to_arrow(
    records: Dict[str, List[Dict[str, Any]]],
    concat: bool = True,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Gather Arrow tables from file-based datasets provided by path.

    Args:
      path: str:
        Where to gather file-based data from.
      concat: bool:  (Default value = True)
        Whether to concatenate similar files together as unified
        datasets.

    Returns:
      Dict[str, List[Dict[str, Any]]]
        Grouped records which include metadata and table data related
        to files which were read.
    """

    for record_group_name, record_group in records.items():
        # if the source datatype is csv, read it as mapped records
        records[record_group_name] = read_files(
            record_group_name=record_group_name, record_group=record_group
        )

    if concat:
        # concat grouped records
        records = concat_records(records=records)

    return records


@flow
def to_parquet(
    records: Dict[str, List[Dict[str, Any]]],
    dest_path: str,
    concat: bool = True,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Export Arrow data to parquet from dataset groups.

    Args:
      records: Dict[str, List[Dict[str, Any]]]:
        Grouped records which include metadata and table data related
        to files which were read.
      dest_path: str:
        Destination where parquet files will be written.
      concat: bool (Default value = True)
        Whether to concatenate similar records together as one.

    Returns:
      Dict[str, List[Dict[str, Any]]]
        Grouped records which include metadata about destination filepath
        where parquet file was written.
    """

    # for each group of records, map writing parquet per file
    for record_group_name, record_group in records.items():
        # check whether we already have tables or if we need a read for records
        if len(
            [key for record in record_group for key in record.keys() if key == "table"]
        ) != len(record_group):
            # read csv's if we have them
            record_group = read_files(
                record_group_name=record_group_name, record_group=record_group
            )

        unique_name = False
        if len(record_group) >= 2:
            unique_name = True

        destinations = write_parquet.map(
            record=record_group, dest_path=dest_path, unique_name=unique_name
        )

        # recollect the group of mapped written records
        records[record_group_name] = [
            destination.result() for destination in destinations
        ]

    if concat:
        records = concat_records(records=records, dest_path=dest_path)

    return {
        key: value.result() if isinstance(value, PrefectFuture) else value
        for key, value in records.items()
    }


def convert(  # pylint: disable=too-many-arguments
    source_path: str,
    dest_path: str,
    dest_datatype: Literal["arrow", "parquet"],
    source_datatype: Optional[str] = None,
    targets: Optional[List[str]] = None,
    concat: bool = True,
    task_runner: BaseTaskRunner = ConcurrentTaskRunner,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Convert file-based data from various sources to Pycytominer-compatible standards.

    Args:
      source_path: str:
        Path to read source files from.
      dest_path: str:
        Path to write files to.
      dest_datatype: Literal["parquet"]:
        Destination datatype to write to.
      source_datatype: Optional[str]:  (Default value = None)
        Source datatype to focus on during conversion.
      targets: Optional[List[str]]:  (Default value = None)
        Target filenames to use for conversion.
      concat: bool:  (Default value = True)
        Whether to concatenate similar files together.
      task_runner: BaseTaskRunner (Default value = ConcurrentTaskRunner)
        Prefect task runner to use with flows.

    Returns:
      Dict[str, List[Dict[str, Any]]]
        Grouped records which include metadata about destination filepath
        where parquet file was written.
    """

    # if we have no targets, set the defaults
    if targets is None:
        targets = DEFAULT_TARGETS

    # gather records to be processed
    records = gather_records.with_options(task_runner=task_runner)(
        path=source_path,
        source_datatype=source_datatype,
        targets=targets,
    )

    # send records to be written to parquet if selected
    if dest_datatype == "arrow":
        output = to_arrow.with_options(task_runner=task_runner)(
            records=records,
            concat=concat,
        )

    # send records to be written to parquet if selected
    elif dest_datatype == "parquet":
        output = to_parquet.with_options(task_runner=task_runner)(
            records=records, concat=concat, dest_path=dest_path
        )

    return output
