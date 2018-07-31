import datetime
from typing import Any, Tuple, Iterable, Dict, Union, Optional, List, Callable

from pathlib import Path
import time
import itertools
from copy import deepcopy, copy

import htcondor
from htcondor import JobAction  # re-import JobAction for users

from . import htcio, utils
from .settings import settings
from . import exceptions


def map(func, args, **kwargs) -> 'MapResult':
    return htcmap(func).map(args, **kwargs)


def productmap(func, *args, **kwargs) -> 'MapResult':
    return htcmap(func).productmap(*args, **kwargs)


def starmap(func, args, kwargs) -> 'MapResult':
    return htcmap(func).starmap(args, kwargs)


def build_job(func):
    return htcmap(func).build_job()


def htcmap(name: Optional[str] = None, submit_descriptors: Optional[Dict] = None):
    def wrapper(func):
        if isinstance(func, HTCMapper):
            func = func.func

        return HTCMapper(
            func,
            name = name if isinstance(name, str) else func.__name__,
            submit_descriptors = submit_descriptors,
        )

    # if called like @htcmap, without parens, name is actually the function
    if callable(name):
        return wrapper(name)

    return wrapper


IndexOrHash = Union[int, str]


class MapResult:
    # todo: specialized versions of query to do condor_q, condor_q --held
    def __init__(self, mapper: 'HTCMapper', clusterid: Optional[int], hashes: Iterable[str]):
        self.mapper = mapper
        self.clusterid = clusterid
        self.hashes = tuple(hashes)
        self.hash_set = set(self.hashes)

        if self.clusterid is None:
            print('no new hashes, no jobs were submitted')

    @classmethod
    def from_clusterid(cls, mapper: 'HTCMapper', clusterid: int):
        with (mapper.hashes_dir / f'{clusterid}.hashes').open() as file:
            return cls(
                mapper = mapper,
                clusterid = clusterid,
                hashes = (h.strip() for h in file),
            )

    def __repr__(self):
        return f'{self.__class__.__name__}(mapper = {self.mapper}, clusterid = {self.clusterid})'

    def _item_to_hash(self, item: IndexOrHash) -> str:
        if isinstance(item, int):
            return self.hashes[item]
        return item

    def __getitem__(self, item: IndexOrHash) -> Any:
        """Non-Blocking get."""
        return self.get(item, timeout = 0)

    def get(
        self,
        item: IndexOrHash,
        timeout: Optional[Union[int, datetime.timedelta]] = None,
    ) -> Any:
        """Blocking get with timeout."""
        if isinstance(timeout, datetime.timedelta):
            timeout = timeout.total_seconds()

        h = self._item_to_hash(item)
        if h not in self.hash_set:
            raise exceptions.HashNotInResult(f'hash {h} is not in this result')

        path = self.mapper.outputs_dir / f'{h}.out'

        try:
            utils.wait_for_path_to_exist(path, timeout)
        except exceptions.TimeoutError as e:
            if timeout <= 0:
                raise exceptions.OutputNotFound(f'output for hash {h} not found')
            else:
                raise e

        return htcio.load_object(path)

    def wait(self, timeout: Optional[Union[int, datetime.timedelta]] = None):
        """Block until ready."""
        start_time = time.time()
        if isinstance(timeout, datetime.timedelta):
            timeout = timeout.total_seconds()

        def is_missing_hashes():
            output_hashes = set(f.stem for f in self.mapper.outputs_dir.iterdir())
            missing_hashes = self.hash_set - output_hashes
            return len(missing_hashes) != 0

        while is_missing_hashes():
            if timeout is not None and time.time() - timeout > start_time:
                raise exceptions.TimeoutError(f'timeout while waiting for {self}')
            time.sleep(1)

    def __iter__(self) -> Iterable[Any]:
        yield from self.iter()

    def iter(
        self,
        callback: Optional[Callable] = None,
        timeout: Optional[Union[int, datetime.timedelta]] = None,
    ) -> Iterable[Any]:
        if callback is None:
            callback = lambda o: o

        for h in self.hashes:
            path = self.mapper.outputs_dir / f'{h}.out'

            utils.wait_for_path_to_exist(path, timeout)

            out = htcio.load_object(path)
            callback(out)
            yield out

    def iter_with_inputs(
        self,
        callback: Optional[Callable] = None,
        timeout: Optional[Union[int, datetime.timedelta]] = None,
    ) -> Iterable[Tuple[Any, Any]]:
        if callback is None:
            callback = lambda i, o: (i, o)

        for h in self.hashes:
            input_path = self.mapper.inputs_dir / f'{h}.in'
            output_path = self.mapper.outputs_dir / f'{h}.out'

            utils.wait_for_path_to_exist(output_path, timeout)

            inp = htcio.load_object(input_path)
            out = htcio.load_object(output_path)
            callback(inp, out)
            yield inp, out

    def iter_as_available(
        self,
        callback: Optional[Callable] = None,
    ) -> Iterable[Any]:
        if callback is None:
            callback = lambda o: o

        paths = {self.mapper.outputs_dir / f'{h}.out' for h in self.hashes}
        while len(paths) > 0:
            for path in copy(paths):
                if not path.exists():
                    continue

                paths.remove(path)
                obj = htcio.load_object(path)
                callback(obj)
                yield obj
            time.sleep(1)

    def iter_as_available_with_inputs(
        self,
        callback: Optional[Callable] = None,
    ) -> Iterable[Tuple[Any, Any]]:
        if callback is None:
            callback = lambda i, o: (i, o)

        paths = {(self.mapper.outputs_dir / f'{h}.out', self.mapper.outputs_dir / f'{h}.out') for h in self.hashes}
        while len(paths) > 0:
            for input_output_paths in copy(paths):
                input_path, output_path = input_output_paths
                if not output_path.exists():
                    continue

                paths.remove(input_output_paths)
                inp = htcio.load_object(input_path)
                out = htcio.load_object(output_path)
                callback(inp, out)
                yield inp, out
            time.sleep(1)

    def query(self, projection: Optional[List[str]] = None):
        if self.clusterid is None:
            yield from ()
        if projection is None:
            projection = []
        yield from htcondor.Schedd().xquery(
            requirements = f'ClusterId=={self.clusterid}',
            projection = projection,
        )

    def act(self, action: JobAction):
        return htcondor.Schedd().act(action, f'ClusterId=={self.clusterid}')

    def remove(self):
        return self.act(JobAction.Remove)

    def iter_output(self, item: IndexOrHash) -> Iterable[str]:
        h = self._item_to_hash(item)
        with (self.mapper.job_logs_dir / f'{h}.out').open() as file:
            yield from file

    def iter_error(self, item: IndexOrHash) -> Iterable[str]:
        h = self._item_to_hash(item)
        with (self.mapper.job_logs_dir / f'{h}.err').open() as file:
            yield from file

    def output(self, item: IndexOrHash):
        return ''.join(self.iter_output(item))

    def error(self, item: IndexOrHash):
        return ''.join(self.iter_error(item))

    def tail(self):
        with (self.mapper.cluster_logs_dir / f'{self.clusterid}.log').open() as file:
            file.seek(0, 2)
            while True:
                current = file.tell()
                line = file.readline()
                if line == '':
                    file.seek(current)
                    time.sleep(.1)
                else:
                    print(line, end = '')


class JobBuilder:
    def __init__(self, mapper: 'HTCMapper'):
        self.mapper = mapper

        self.args = []
        self.kwargs = []

        self.result = None

    def __repr__(self):
        return f'{self.__class__.__name__}(mapper = {self.mapper})'

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # todo: should do nothing if exception occurred inside with block
        self.result = self.mapper.starmap(self.args, self.kwargs)

    def __call__(self, *args, **kwargs):
        self.args.append(args)
        self.kwargs.append(kwargs)

    @property
    def result(self):
        if self._result is None:
            raise exceptions.NoResultYet('result does not exist until after with block')
        return self._result

    @result.setter
    def result(self, result: MapResult):
        self._result = result

    def __len__(self):
        return len(self.args)


class HTCMapper:
    def __init__(self, func: Callable, name: str, submit_descriptors = None):
        self.func = func
        self.name = name
        self.submit_descriptors = submit_descriptors or {}

        self.map_dir = settings.HTCMAP_DIR / name
        self.inputs_dir = self.map_dir / 'inputs'
        self.outputs_dir = self.map_dir / 'outputs'
        self.job_logs_dir = self.map_dir / 'job_logs'
        self.cluster_logs_dir = self.map_dir / 'cluster_logs'
        self.hashes_dir = self.map_dir / 'hashes_by_clusterid'

        self._mkdirs()

        self.fn_path = self.map_dir / 'fn.pkl'
        if not self.fn_path.exists():
            htcio.save_object(self.func, self.fn_path)

    def _mkdirs(self):
        for path in (
            self.map_dir,
            self.inputs_dir,
            self.outputs_dir,
            self.job_logs_dir,
            self.cluster_logs_dir,
            self.hashes_dir,
        ):
            path.mkdir(parents = True, exist_ok = True)

    def __repr__(self):
        return f'{self.__class__.__name__}(name = {self.name}, func = {self.func})'

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    def map(self, args, **kwargs) -> MapResult:
        args = ((arg,) for arg in args)
        args_and_kwargs = zip(args, itertools.repeat(kwargs))
        return self._map(args_and_kwargs)

    def productmap(self, *args, **kwargs) -> MapResult:
        dicts = [{}]
        for key, values in kwargs.items():
            values = tuple(values)
            dicts = [deepcopy(d) for d in dicts for _ in range(len(values))]
            for d, v in zip(dicts, itertools.cycle(values)):
                d[key] = v

        args = itertools.repeat(args)
        args_and_kwargs = zip(args, dicts)

        return self._map(args_and_kwargs)

    def starmap(self, args: Iterable[Tuple] = (), kwargs: Iterable[Dict] = ()) -> MapResult:
        args_and_kwargs = zip_args_and_kwargs(args, kwargs)
        return self._map(args_and_kwargs)

    def build_job(self):
        return JobBuilder(mapper = self)

    def _map(self, args_and_kwargs) -> MapResult:
        hashes = []
        new_hashes = []
        for a_and_k in args_and_kwargs:
            b = htcio.to_bytes(a_and_k)
            h = htcio.hash_bytes(b)
            hashes.append(h)

            # if output already exists, don't re-do it
            output_path = self.outputs_dir / f'{h}.out'
            if output_path.exists():
                continue

            input_path = self.inputs_dir / f'{h}.in'
            htcio.save_bytes(b, input_path)
            new_hashes.append(h)

        if len(new_hashes) == 0:
            return MapResult(
                mapper = self,
                clusterid = None,
                hashes = hashes,
            )

        submit_dict = {
            'JobBatchName': self.name,
            'executable': str(Path(__file__).parent / 'run' / 'run.sh'),
            'arguments': '$(Item)',
            'log': str(self.cluster_logs_dir / '$(ClusterId).log'),
            'output': str(self.job_logs_dir / '$(Item).output'),
            'error': str(self.job_logs_dir / '$(Item).error'),
            'should_transfer_files': 'YES',
            'when_to_transfer_output': 'ON_EXIT',
            'request_cpus': '1',
            'request_memory': '100MB',
            'request_disk': '5GB',
            'transfer_input_files': ','.join([
                'http://proxy.chtc.wisc.edu/SQUID/karpel/htcmap.tar.gz',
                str(Path(__file__).parent / 'run' / 'run.py'),
                str(self.inputs_dir / '$(Item).in'),
                str(self.fn_path),
            ]), 'transfer_output_remaps': '"' + ';'.join([
                f'$(Item).out={self.outputs_dir / "$(Item).out"}',
            ]) + '"'
        }
        sub = htcondor.Submit(submit_dict)

        schedd = htcondor.Schedd()
        with schedd.transaction() as txn:
            submit_result = sub.queue_with_itemdata(txn, 1, iter(new_hashes))

        clusterid = submit_result.cluster()

        with (self.hashes_dir / f'{clusterid}.hashes').open(mode = 'w') as file:
            file.write('\n'.join(hashes))

        return MapResult(
            mapper = self,
            clusterid = clusterid,
            hashes = hashes,
        )

    def reconstruct(self, clusterid: int):
        return MapResult.from_clusterid(self, clusterid)

    def clean(self) -> (int, int):
        outs = (
            self.clean_inputs(),
            self.clean_outputs(),
            self.clean_job_logs(),
            self.clean_cluster_logs(),
        )

        num_files = sum(o[0] for o in outs)
        num_bytes = sum(o[1] for o in outs)

        return num_files, num_bytes

    def clean_inputs(self) -> (int, int):
        return utils.clean_dir(self.inputs_dir)

    def clean_outputs(self) -> (int, int):
        return utils.clean_dir(self.outputs_dir)

    def clean_job_logs(self) -> (int, int):
        return utils.clean_dir(self.job_logs_dir)

    def clean_cluster_logs(self) -> (int, int):
        return utils.clean_dir(self.cluster_logs_dir)


def zip_args_and_kwargs(args: Iterable[Tuple], kwargs: Iterable[Dict]):
    iterators = [iter(args), iter(kwargs)]
    fills = {0: (), 1: {}}
    num_active = 2
    while True:
        values = []
        for i, it in enumerate(iterators):
            try:
                value = next(it)
            except StopIteration:
                num_active -= 1
                if num_active == 0:
                    return
                iterators[i] = itertools.repeat(fills[i])
                value = fills[i]
            values.append(value)
        yield tuple(values)