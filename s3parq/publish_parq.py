import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import s3fs
import sys
import logging
from typing import List

from .session_helper import SessionHelper
from sqlalchemy import Column, Integer, String
## Commenting to wait and see how rest of team implements using SH
# SH = SessionHelper({some_data})
# SH.configure_session_helper()


logger = logging.getLogger(__name__)


def check_empty_dataframe(dataframe: pd.DataFrame) -> None:
    """ Make sure that the dataframe being published is not empty. """
    if dataframe.empty:
        no_data_message = "Empty dataframes cannot be published."
        raise ValueError(no_data_message)


def check_dataframe_for_timedelta(dataframe: pd.DataFrame)->None:
    """Pyarrow can't do timedelta."""
    dtypes = set(dataframe.columns)
    for dtype in dtypes:
        if 'timedelta' in dtype:
            timedelta_message = "Pyarrow does not support parquet conversion of timedelta columns at this time."
            logger.debug(timedelta_message)
            raise NotImplementedError(timedelta_message)


def _check_partition_compatibility(partition: str) -> bool:
    """ Make sure each partition value is hive-allowed."""
    reserved = "ALL, ALTER, AND, ARRAY, AS, AUTHORIZATION, BETWEEN, BIGINT, BINARY, BOOLEAN, BOTH, BY, CASE, CAST, CHAR, COLUMN, CONF, CREATE, CROSS, CUBE, CURRENT, CURRENT_DATE, CURRENT_TIMESTAMP, CURSOR, DATABASE, DATE, DECIMAL, DELETE, DESCRIBE, DISTINCT, DOUBLE, DROP, ELSE, END, EXCHANGE, EXISTS, EXTENDED, EXTERNAL, FALSE, FETCH, FLOAT, FOLLOWING, FOR, FROM, FULL, FUNCTION, GRANT, GROUP, GROUPING, HAVING, IF, IMPORT, IN, INNER, INSERT, INT, INTERSECT, INTERVAL, INTO, IS, JOIN, LATERAL, LEFT, LESS, LIKE, LOCAL, MACRO, MAP, MORE, NONE, NOT, NULL, OF, ON, OR, ORDER, OUT, OUTER, OVER, PARTIALSCAN, PARTITION, PERCENT, PRECEDING, PRESERVE, PROCEDURE, RANGE, READS, REDUCE, REVOKE, RIGHT, ROLLUP, ROW, ROWS, SELECT, SET, SMALLINT, TABLE, TABLESAMPLE, THEN, TIMESTAMP, TO, TRANSFORM, TRIGGER, TRUE, TRUNCATE, UNBOUNDED, UNION, UNIQUEJOIN, UPDATE, USER, USING, UTC_TMESTAMP, VALUES, VARCHAR, WHEN, WHERE, WINDOW, WITH, COMMIT, ONLY, REGEXP, RLIKE, ROLLBACK, START, CACHE, CONSTRAINT, FOREIGN, PRIMARY, REFERENCES, DAYOFWEEK, EXTRACT, FLOOR, INTEGER, PRECISION, VIEWS, TIME, NUMERIC, SYNC".split()
    reserved = [x.strip(',') for x in reserved]
    return not (partition.upper() in reserved)


def check_partitions(partitions: iter, dataframe: pd.DataFrame)->None:
    logger.debug("Checking partition args...")
    for partition in partitions:
        if partition not in dataframe.columns.tolist():
            partition_message = f"Cannot set {partition} as a partition; this is not a valid column header for the supplied dataframe."
            logger.critical(partition_message)
            raise ValueError(partition_message)
        if not _check_partition_compatibility(partition):
            partition_message = f"{partition} is a reserved word in hive that cannot be used as a partition."
            logger.critical(partition_message)
            raise ValueError(partition_message)
    logger.debug("Done checking partitions.")


def s3_url(bucket: str, key: str):
    return '/'.join(["s3:/", bucket, key])


def _get_partitions_for_spectrum(filename: str) -> [str]:
    '''
    Turns S3 filepath with partitions into list of only those partitions as strings
    Args:
        filename (str): entire filepath for a single file
    ----
    Returns:
        final_partitions (list of strings): these are the partitions for that file
    --------
    Example:
        Args:
            filename = 'some-path/to/data/zipcode=12345/birth_month=january/final_data_set.parquet
        ----
        Returns:
            final_partitions = ['zipcode=12345', 'birth_month=january']
    '''
    filepath = filename.split('/')
    final_partitions = [_dir for _dir in filepath if '=' in _dir]
    return final_partitions


def _format_partition_strings_for_sql(partitions: [str]) -> [str]:
    '''
    Formats a list of S3 partition strings for SQL statements
    Args:
        partitions ([str]): list of strings representing the partitions in S3
    ----
    Returns:
        formatted_partitions ([str]): list of the same partitions with quotes
        to use for SQL
    --------
    Example:
        Args:
            partitions = ['hamburger=abcd', 'hot_dog=1234']
        ----
        Returns:
            formatted_partitions = ["hamburger='abcd'", "hot_dog='1234'"]
    '''
    formatted_partitions = []
    for p in partitions:
        key, value = p.split('=')
        value = f"'{value}'"
        formatted_partitions.append(f'{key}={value}')
    return formatted_partitions


def last_index_containing_substring(the_list: [str], substring: str) -> int:
    '''
    Returns index of last string that contains a substring within a list.  If there's no match, it returns
    the length of the list + 1 because we want the function that uses it "_get_partition_location"
    to not identify any partitions if none exists
    Args:
        the_list ([str]): list of strings, probably representing the partitions
        substring (str): string to look for in the_list
    ----
    Returns:
        i (int): The index in the list that contains the first instance of the substring
        returns -1 if no matches
    --------
    Example:
        Args: 
            the_list = ['path', 'to', 'data', 'hamburger=abcd', 'hot_dog=1234', 'the_file.parquet]
            substring = '='
        ----
        Returns:
            i = 3
        
    '''
    for s in reversed(the_list):
        if substring in s:
            return len(the_list) - the_list.index(s)
    return len(the_list) + 1


def _get_partition_location(filepath: str):
    '''
    Gets location of data in S3 for partitioning in S3.  You need to know the path to 
    the first partition in order to make a proper Spectrum partition w/ SQL-Redshift
    Args:
        filepath (str): path to a file in S3 that is partitioned
    Returns:
        final_path (str): path within S3 bucket that has the last partition of a filepath
    Example:
        Args: 
            filepath = 'path/to/data/hamburger=abcd/hot_dog=1234/abcd1234.parquet'
        Returns:
            final_path = 'path/to/data/hamburger=abcd/hot_dog=1234'
    '''
    separate_dirs = filepath.split('/')
    last_partition = last_index_containing_substring(separate_dirs, "=")
    final_set = separate_dirs[:-last_partition + 1] #  I think this is more confusing than it has to be
    final_path = '/'.join(final_set)
    if final_path == '':
        raise ValueError(f'No partitions in this filepath {filepath}')
    return final_path


def _generate_partition_sql(bucket: str, schema: str, table: str, filepath: str) -> str:
    '''
    Generates partitioning SQL
    Args:
        bucket (str): S3 bucket where data is stored
        schema (str): name of redshift schema (must already exist)
        table (str): name of table in schema.  Must have partitions scoped out in `CREATE TABLE ...`
        filepath (str): path to data in S3 that will be queryable by it's partitions
    ----
    Returns:
        query (str): a raw SQL string that adds the partitioned data to the table
    --------
    Example:
        Args:
            bucket = 'MyBucket'
            schema = 'MySchema'
            table = 'MyTable'
            filepath = 'path/to/data/apple=abcd/banana=1234/abcd1234.parquet'
        Returns:
            "ALTER TABLE MySchema.MyTable               ADD PARTITION (apple='abcd' ,banana='1234')               LOCATION 's3://MyBucket/path/to/data/apple=abcd';"
    '''
    partitions = _get_partitions_for_spectrum(filepath)
    formatted_partitions = _format_partition_strings_for_sql(partitions)
    path_to_data = _get_partition_location(filepath)
    query = f"ALTER TABLE {schema}.{table} \
              ADD PARTITION ({' ,'.join(formatted_partitions)}) \
              LOCATION 's3://{bucket}/{path_to_data}';"
    return query

def _gen_parquet_to_s3(bucket: str, key: str, dataframe: pd.DataFrame,
                       partitions: list) -> None:
    """ pushes the parquet dataset directly to s3. """
    logger.info("Writing to S3...")
    table = pa.Table.from_pandas(dataframe, preserve_index=False)

    uri = s3_url(bucket, key)
    logger.debug(f"Writing to s3 location: {uri}...")
    pq.write_to_dataset(table, compression="snappy", root_path=uri,
                        partition_cols=partitions, filesystem=s3fs.S3FileSystem())
    logger.debug("Done writing to location.")


def _assign_partition_meta(bucket: str, key: str, dataframe: pd.DataFrame, partitions: iter, write_to_redshift: bool = False) -> List[str]:
    """ assigns the dataset partition meta to all keys in the dataset"""
    s3_client = boto3.client('s3')
    all_files_without_meta = []
    paginator = s3_client.get_paginator('list_objects')
    page_iterator = paginator.paginate(Bucket=bucket, Prefix=key)
    for page in page_iterator:
        for obj in page['Contents']:
            if obj['Key'].endswith(".parquet"):
                head_obj = s3_client.head_object(Bucket=bucket, Key=obj['Key'])
                if not 'partition_data_types' in head_obj['Metadata']:
                    all_files_without_meta.append(obj['Key'])
                if write_to_redshift:
                    sql_command = _generate_partition_sql(bucket, 'spectrum', 'test12', obj['Key'])
                    print(sql_command)
                    # with SH.db_session_scope() as scope:
                    #     scope.execute(sql_command)
    

    for obj in all_files_without_meta:
        logger.debug(f"Appending metadata to file {obj}..")
        s3_client.copy_object(Bucket=bucket, CopySource={'Bucket': bucket, 'Key': obj}, Key=obj,
                              Metadata={'partition_data_types': str(
                                  _parse_dataframe_col_types(
                                      dataframe=dataframe, partitions=partitions)
                              )}, MetadataDirective='REPLACE')
        logger.debug("Done appending metadata.")
    return all_files_without_meta


def _parse_dataframe_col_types(dataframe: pd.DataFrame, partitions: list) -> dict:
    """ Returns a dict with the column names as keys, the data types (in strings) as values."""
    logger.debug("Determining write metadata for publish...")
    dataframe = dataframe[partitions]
    dtypes = {}
    for col, dtype in dataframe.dtypes.items():
        dtype = str(dtype)
        if dtype == 'object':
            dtypes[col] = 'string'
        elif dtype.startswith('int'):
            dtypes[col] = 'integer'
        elif dtype.startswith('float'):
            dtypes[col] = 'float'
        elif dtype.startswith('date'):
            dtypes[col] = 'datetime'
        elif dtype.startswith('category'):
            dtypes[col] = 'category'
        elif dtype == 'bool':
            dtypes[col] = 'boolean'
    logger.debug(f"Done.Metadata set as {dtypes}")
    return dtypes


def _sized_dataframes(dataframe: pd.DataFrame) -> tuple:
    """Takes a dataframe and slices it into sized dataframes for optimal parquet sizes in S3.
        RETURNS a tuple of dataframes.         
    """

    def log_size_estimate(num_bytes):
        if num_bytes == 0:
            return "0MB"
        elif round(num_bytes / float(1 << 20), 2) == 0.0:
            return "<0.1MB"
        else:
            return str(round(num_bytes / float(1 << 20), 2)) + "MB"

    # get first row size
    row_size_est = sys.getsizeof(dataframe.head(1))
    # get number of rows
    num_rows = int(dataframe.shape[0])
    frame_size_est = row_size_est * num_rows
    # at scale dataframes seem to compress around 3.5-4.5 times as parquet.
    # TODO: should build a real regression to calc this!
    compression_ratio = 4
    # 60MB compressed is ideal for Spectrum
    ideal_size = compression_ratio * (60 * float(1 << 20))
    # short circut if < ideal size
    batch_log_message = """row size estimate: {row_size_est} bytes.
number of rows: {num_rows} rows
frame size estimate: {frame_size_est} bytes
compression ratio: {compression_ratio}:1
ideal size: {ideal_size} bytes
"""
    logger.debug(batch_log_message)
    if ideal_size > frame_size_est:
        return tuple([dataframe])

    # math the number of estimated partitions
    sized_frames = []
    num_partitions = int(row_size_est * num_rows / ideal_size)
    rows_per_partition = int(num_rows / num_partitions)
    # for each partition do the thing
    for index, lower in enumerate(range(0, num_rows, rows_per_partition)):
        lower = lower if lower == 0 else lower + 1
        if index + 1 == num_partitions:
            upper = num_rows
        else:
            upper = lower + rows_per_partition
        sized_frames.append(dataframe[lower:upper])
    logger.info(f"sized out {len(sized_frames)} dataframes.")
    return tuple(sized_frames)


def publish(bucket: str, key: str, partitions: iter, dataframe: pd.DataFrame) -> None:
    logger.info("Checking params....")
    check_empty_dataframe(dataframe)
    check_dataframe_for_timedelta(dataframe)
    check_partitions(partitions, dataframe)
    logger.info("Params valid.")
    logger.debug("Begin writing to S3..")

    files = []
    for frame in _sized_dataframes(dataframe):
        _gen_parquet_to_s3(bucket=bucket,
                           key=key,
                           dataframe=dataframe,
                           partitions=partitions)

        published_files = _assign_partition_meta(bucket=bucket,
                                                 key=key,
                                                 dataframe=dataframe,
                                                 partitions=partitions,
                                                 write_to_redshift=True)
        files = files + published_files
    logger.debug("Done writing to S3.")
    return files
