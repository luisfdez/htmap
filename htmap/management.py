# Copyright 2018 HTCondor Team, Computer Sciences Department,
# University of Wisconsin-Madison, WI.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Tuple, Iterator, Iterable, Dict, Union, NamedTuple
import logging

from pathlib import Path
import shutil
import datetime
import json
import csv
import io
import textwrap

from . import mapping, utils, settings, exceptions
from .maps import Map, ComponentStatus

logger = logging.getLogger(__name__)


def load(map_id: str) -> Map:
    """
    Reconstruct a :class:`Map` from its ``map_id``.

    Parameters
    ----------
    map_id
        The ``map_id`` to search for.

    Returns
    -------
    map
        The result with the given ``map_id``.
    """
    return Map.load(map_id)


def _map_paths() -> Iterator[Path]:
    """Yield the paths to all existing map directories."""
    try:
        yield from mapping.maps_dir_path().iterdir()
    except FileNotFoundError:  # maps dir doesn't exist for some reason, which means we have no maps
        return


def map_ids() -> Tuple[str, ...]:
    """Return a tuple containing the ``map_id`` for all existing maps."""
    return tuple(path.name for path in _map_paths())


def load_maps() -> Tuple[Map, ...]:
    """Return a :class:`tuple` containing the :class:`Map` for all existing maps."""
    return tuple(load(map_id) for map_id in map_ids())


def remove(map_id: str, not_exist_ok: bool = True) -> None:
    """
    Remove the map with the given ``map_id``.

    Parameters
    ----------
    map_id
        The ``map_id`` to search for and remove.
    not_exist_ok
        If ``False``, raise :class:`htmap.exceptions.MapIdNotFound` if the ``map_id`` doesn't exist.
    """
    try:
        r = load(map_id)
        r.remove()
    except (exceptions.MapIdNotFound, FileNotFoundError) as e:
        if not not_exist_ok:
            if not isinstance(e, exceptions.MapIdNotFound):
                raise exceptions.MapIdNotFound(f'map {map_id} not found') from e
            raise e


def force_remove(map_id: str) -> None:
    """
    Force-remove a map by trying to delete its map directory directly.

    .. warning::

        This operation is **not safe**, but might be necessary if your map directory has somehow become corrupted.
        See :ref:`cleanup-after-force-removal`.

    Parameters
    ----------
    map_id
        The ``map_id`` to force-remove.
    """
    shutil.rmtree(str(mapping.map_dir_path(map_id).absolute()), ignore_errors = True)
    logger.debug(f'force-removed map {map_id}')


def clean() -> None:
    """Remove all existing maps."""
    logger.debug('cleaning maps directory...')
    for map_result in load_maps():
        map_result.remove()
    logger.debug('cleaned maps directory')


def force_clean() -> None:
    """
    Force-remove all existing maps by trying to delete their map directories directly.

    .. warning::

        This operation is **not safe**, but might be necessary if your map directory has somehow become corrupted.
        See :ref:`cleanup-after-force-removal`.
    """
    for map_dir in _map_paths():
        shutil.rmtree(str(map_dir.absolute()))

    logger.debug('force-cleaned maps directory')


def status(
    maps: Iterable[Map] = None,
    state: bool = True,
    meta: bool = True,
) -> str:
    """
    Return a formatted table containing information on the given maps.

    Parameters
    ----------
    maps
        The maps to display information on.
        If ``None``, displays information on all existing maps.
    state
        If ``True``, include information on the state of the map's components.
    meta
        If ``True``, include information about the map's memory usage, disk usage, and runtime.

    Returns
    -------
    table :
        A text table containing information on the given maps.
    """
    if maps is None:
        maps = load_maps()

    maps = sorted(maps, key = lambda m: m.map_id)

    headers = ['Map ID']
    if state:
        utils.read_events(maps)
        headers += [str(d) for d in ComponentStatus.display_statuses()]
    if meta:
        headers += ['Local Data', 'Max Memory', 'Max Runtime', 'Total Runtime']

    rows = []
    for map in maps:
        row = [map.map_id]
        if state:
            row.extend(str(map.status_counts[d]) for d in ComponentStatus.display_statuses())
        if meta:
            row.extend([
                utils.get_dir_size_as_str(mapping.map_dir_path(map.map_id)),
                utils.num_bytes_to_str(max(map.memory_usage) * 1024 * 1024),  # memory usage is measured in MB
                str(max(map.runtime)),
                str(sum(map.runtime, datetime.timedelta())),
            ])

        rows.append(row)

    return utils.table(
        headers = headers,
        rows = rows,
    )


def status_json(
    maps: Iterable[Map] = None,
    state: bool = True,
    meta: bool = True,
    compact: bool = False,
) -> str:
    """
    Return a JSON-formatted string containing information on the given maps.

    Disk and memory usage are reported in bytes.
    Runtimes are reported in seconds.

    Parameters
    ----------
    maps
        The maps to display information on.
        If ``None``, displays information on all existing maps.
    state
        If ``True``, include information on the state of the map's components.
    meta
        If ``True``, include information about the map's memory usage, disk usage, and runtime.
    compact
        If ``True``, the JSON will be formatted in the most compact possible representation.

    Returns
    -------
    json :
        A JSON-formatted dictionary containing information on the given maps.
    """
    if maps is None:
        maps = load_maps()

    maps = sorted(maps, key = lambda m: m.map_id)

    if state:
        utils.read_events(maps)

    j = {}
    for map in maps:
        d: Dict[str, Union[dict, str, int, float]] = {'map_id': map.map_id}
        if state:
            status_to_count = {}
            for status in ComponentStatus.display_statuses():
                status_to_count[status.value.lower()] = map.status_counts[status]
            d['component_status_counts'] = status_to_count
        if meta:
            d['local_disk_usage'] = utils.get_dir_size(mapping.map_dir_path(map.map_id))
            d['max_memory_usage'] = max(map.memory_usage) * 1024 * 1024
            d['max_runtime'] = max(map.runtime).total_seconds()
            d['total_runtime'] = sum(map.runtime, datetime.timedelta()).total_seconds()

        j[map.map_id] = d

    if compact:
        separators = (',', ':')
        indent = 0
    else:
        separators = (', ', ': ')
        indent = 4

    return json.dumps(j, separators = separators, indent = indent)


def status_csv(
    maps: Iterable[Map] = None,
    state: bool = True,
    meta: bool = True,
) -> str:
    """
    Return a CSV-formatted string containing information on the given maps.

    Disk and memory usage are reported in bytes.
    Runtimes are reported in seconds.

    Parameters
    ----------
    maps
        The maps to display information on.
        If ``None``, displays information on all existing maps.
    state
        If ``True``, include information on the state of the map's components.
    meta
        If ``True``, include information about the map's memory usage, disk usage, and runtime.

    Returns
    -------
    csv :
        A CSV-formatted table containing information on the given maps.
    """
    if maps is None:
        maps = load_maps()

    maps = sorted(maps, key = lambda m: m.map_id)

    if state:
        utils.read_events(maps)

    rows = []
    for map in maps:
        row: Dict[str, Union[str, int, float]] = {'map_id': map.map_id}
        if state:
            for status in ComponentStatus.display_statuses():
                row[status.value.lower()] = map.status_counts[status]
        if meta:
            row['local_disk_usage'] = utils.get_dir_size(mapping.map_dir_path(map.map_id))
            row['max_memory_usage'] = max(map.memory_usage) * 1024 * 1024
            row['max_runtime'] = max(map.runtime).total_seconds()
            row['total_runtime'] = sum(map.runtime, datetime.timedelta()).total_seconds()

        rows.append(row)

    if len(maps) == 0:
        return ''

    output = io.StringIO()
    writer = csv.DictWriter(output, list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)

    return output.getvalue()


class Transplant(NamedTuple):
    hash: str
    path: Path
    created: datetime.datetime
    size: int
    packages: Tuple[str]

    @classmethod
    def load(cls, path: Path):
        """

        Parameters
        ----------
        path
            The path to the transplant install.

        Returns
        -------

        """

        return cls(
            hash = path.stem,
            path = path,
            created = datetime.datetime.fromtimestamp(path.stat().st_ctime),
            size = path.stat().st_size,
            packages = tuple(path.with_suffix('.pip').read_text().strip().split('\n')),
        )

    def remove(self):
        self.path.with_suffix('.pip').unlink()
        self.path.unlink()
        logger.info(f'removed transplant install {self.hash}, which was created at {self.created}')


def transplants() -> Tuple[Transplant, ...]:
    return tuple(sorted(
        (
            Transplant.load(p)
            for p in Path(settings['TRANSPLANT.DIR']).iterdir()
            if p.suffix != '.pip'
        ),
        key = lambda t: t.created,
    ))


def transplant_info() -> str:
    entries = []
    for q, t in enumerate(transplants()):
        packages = '\n'.join(
            textwrap.wrap(
                ', '.join(t.packages),
                subsequent_indent = ' ' * 4,
                break_long_words = False,
            )
        )
        entry = f'# {q}\nHash: {t.hash}\nCreated at: {t.created}\nPackages: {packages}'
        entries.append(entry)

    return utils.rstr('\n\n'.join(entries))
