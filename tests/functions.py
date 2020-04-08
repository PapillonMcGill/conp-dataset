from contextlib import contextmanager
import json
import os
from random import sample
import re
import signal
import sys
from typing import List, Tuple

import datalad.api as api
import git
import keyring
import pytest

from scripts.dats_validator.validator import validate_json


@contextmanager
def timeout(time):
    # Register a function to raise a TimeoutError on the signal.
    signal.signal(signal.SIGALRM, raise_timeout)
    # Schedule the signal to be sent after ``time``.
    signal.alarm(time)

    try:
        yield
    except TimeoutError:
        pass
    finally:
        # Unregister the signal so it won't be triggered
        # if the timeout is not reached.
        signal.signal(signal.SIGALRM, signal.SIG_IGN)


def raise_timeout(signum, frame):
    raise TimeoutError


def get_annexed_file_size(dataset, file_path):
    """Get the size of an annexed file in Bytes.
    
    Parameters
    ----------
    dataset : string
        Path to the dataset containing the file.
    file_path : str
        Realative path of the file within a dataset
    
    Returns
    -------
    float
        Size of the annexed file in Bytes.
    """
    attempt = 0
    while attempt < 3:
        metadata = json.loads(
            git.Repo(dataset).git.annex(
                "info", os.path.join(dataset, file_path), json=True, bytes=True,
            )
        )
        if "size" in metadata:
            break
        attempt += 1
    else:
        # Failed all attempt
        return float("inf")

    return int(metadata["size"])


def get_small_files(
    dataset: str, filenames: List[str], num_files: int
) -> List[Tuple[str, int]]:
    """Retrieve a list of small files from the given list.
    
    We want to download small files to prevent timeout, however, we cannot find the 
    n smallest files as this would also result in timeout for dataset with a large
    amount of files.

    This greedy method allows to retrieve small files rapidly and fall back on returning
    any file size in the worst case.

    Parameters
    ----------
    dataset : str
        Path to the dataset.
    filenames : List[str]
        Path of the dataset files.
    num_files : int
        Maximum number of files to test.
    
    Returns
    -------
    List[Tuple[str, int]]
        Files of small size.
    """
    small_files: List[Tuple[str, int]] = []
    SMALL_SIZE: int = 2 ** 7  # 10 MB

    medium_files: List[Tuple[str, int]] = []
    MEDIUM_SIZE: int = 2 ** 8  # 100 MB

    large_files: List[Tuple[str, int]] = []
    LARGE_SIZE: int = 2 ** 9  # 1 GB

    other_files: List[Tuple[str, int]] = []

    for filename in filenames:
        file_size: int = get_annexed_file_size(dataset, filename)

        if file_size < SMALL_SIZE:
            small_files.append((filename, file_size))
            if len(small_files) == num_files:
                return sorted(small_files, key=lambda x: x[1])
        elif file_size < MEDIUM_SIZE:
            medium_files.append((filename, file_size))
            if len(small_files) + len(medium_files) == num_files:
                return sorted(small_files, key=lambda x: x[1]) + sorted(
                    medium_files, key=lambda x: x[1]
                )
        elif file_size < LARGE_SIZE:
            large_files.append((filename, file_size))
            if len(small_files) + len(medium_files) + len(large_files) == num_files:
                return (
                    sorted(small_files, key=lambda x: x[1])
                    + sorted(medium_files, key=lambda x: x[1])
                    + sorted(large_files, key=lambda x: x[1])
                )
        else:
            other_files.append((filename, file_size))

    # Not enough small file at this point
    nb_file_missing: int = num_files - (
        len(small_files) + len(medium_files) + len(large_files)
    )
    return (
        sorted(small_files)
        + sorted(medium_files)
        + sorted(large_files)
        + other_files[:nb_file_missing]
    )


def remove_ftp_files(dataset: str, filenames: list) -> list:
    """Remove files that only use ftp as a remote.
    
    Parameters
    ----------
    dataset : str
        Path to the dataset containing the files.
    filenames : List[str]
        List of filenames path in the dataset.
    
    Returns
    -------
    files_without_ftp : list
        List of filenames path not using ftp.
    """
    files_without_ftp = []
    for filename in filenames:
        whereis = json.loads(
            git.Repo(dataset).git.annex(
                "whereis", os.path.join(dataset, filename), json=True
            )
        )

        urls_without_ftp = [
            url
            for x in whereis["whereis"]
            for url in x["urls"]
            if not url.startswith("ftp://")
        ]

        if len(urls_without_ftp) > 0:
            files_without_ftp.append(filename)

    return files_without_ftp


def is_authentication_required(dataset):
    """Verify in the dataset DATS file if authentication is required.
    
    Parameters
    ----------
    dataset : str
        Relative path to the dataset root.
    
    Returns
    -------
    bool
        Wether the dataset requires authentication.
    """
    with open(os.path.join(dataset, "DATS.json"), "rb") as fin:
        metadata = json.load(fin)

        try:
            distributions = metadata["distributions"]
            for distrubtion in distributions:
                authorizations = distrubtion["access"]["authorizations"]
                if any(
                    [
                        authorization["value"] != "public"
                        for authorization in authorizations
                    ]
                ):
                    return True

            return False
        except KeyError as e:
            return str(e) + " DATS.json is invalid!"


def generate_datalad_provider(loris_api):

    # Regex for provider
    re_loris_api = loris_api.replace(".", "\.")

    with open(
        os.path.join(
            os.path.expanduser("~"), ".config", "datalad", "providers", "loris.cfg"
        ),
        "w",
    ) as fout:
        fout.write(
            f"""[provider:loris]                                                                    
url_re = {re_loris_api}/*                             
authentication_type = loris-token                                                           
credential = loris                                                                  
                                                                                            
[credential:loris]                                                                  
url = {loris_api}/login                                           
type = loris-token  
"""
        )


def examine(dataset, project):
    repo = git.Repo(dataset)

    # Make sure the dataset is sync to latest version.
    repo.git.pull("origin", "master")

    file_names = [file_name for file_name in os.listdir(dataset)]

    if "README.md" not in file_names:
        pytest.fail(
            f"Dataset {dataset} doesn't contain README.md in its root directory.",
            pytrace=False,
        )

    if "DATS.json" not in file_names:
        pytest.fail(
            f"Dataset {dataset} doesn't contain DATS.json in its root directory.",
            pytrace=False,
        )

    with open(os.path.join(dataset, "DATS.json"), "rb") as f:
        if not validate_json(json.load(f)):
            pytest.fail(
                f"Dataset {dataset} doesn't contain a valid DATS.json.", pytrace=False
            )

    # If authentication is required and credentials are provided then add credentials
    # to the keyring and create a provider config file.
    # Note: Assume a loris-token authentication.
    username = os.getenv(project + "_USERNAME", None)
    password = os.getenv(project + "_PASSWORD", None)
    loris_api = os.getenv(project + "_LORIS_API", None)

    if username and password and loris_api:
        keyring.set_password("datalad-loris", "user", username)
        keyring.set_password("datalad-loris", "password", password)
        generate_datalad_provider(loris_api)
    elif is_authentication_required(dataset) == True:
        if os.getenv("TRAVIS_EVENT_TYPE", None) == "pull_request" or os.getenv(
            "CIRCLE_PR_NUMBER", False
        ):
            pytest.skip(
                f"WARNING: {dataset} cannot be test on Pull Requests to protect secrets."
            )

        pytest.fail(
            "Cannot download file (dataset requires authentication, make sure "
            + f"that environment variables {project}_USERNAME, {project}_PASSWORD, "
            + f"and {project}_LORIS_API are defined in Travis).",
            pytrace=False,
        )

    annex_list: str = repo.git.annex("list")
    filenames: list = re.split(r"\n[_X]+\s", annex_list)[1:]

    if len(filenames) == 0:
        pytest.skip(f"WARNING: {dataset} No files found in the annex.")

    # Remove files using FTP as it is unstable in travis.
    if os.getenv("TRAVIS", False):
        filenames = remove_ftp_files(dataset, filenames)

        if len(filenames) == 0:
            pytest.skip(
                f"WARNING: {dataset} only contains files using FTP."
                + " Due to Travis limitation we cannot test this dataset."
            )

    filenames = get_small_files(dataset, filenames, 4)
    responses = []
    TIMEOUT = 120
    with timeout(TIMEOUT):
        for filename, file_size in filenames:
            full_path = os.path.join(dataset, filename)
            responses = api.get(path=full_path, on_failure="ignore")

            for response in responses:
                if response.get("status") in ["ok", "notneeded"]:
                    continue
                if response.get("status") in ["impossible", "error"]:
                    pytest.fail(
                        f"{full_path}\n{response.get('message')}", pytrace=False
                    )

    if responses == []:
        pytest.fail(
            f"The dataset timed out after {TIMEOUT} seconds before retrieving a file."
            + " Cannot to tell if the download would be sucessful."
            + f"\n{filename} has size of {file_size} Bytes.",
            pytrace=False,
        )

    return True
