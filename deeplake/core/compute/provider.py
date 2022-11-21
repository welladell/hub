from abc import ABC, abstractmethod
from typing import Optional


class ComputeProvider(ABC):
    """An abstract base class for implementing a compute provider."""

    def __init__(self, workers):
        self.workers = workers

    def get_progressbar(self, total_length, desc):
        import warnings
        from tqdm.std import tqdm  # type: ignore
        from tqdm import TqdmWarning

        warnings.simplefilter("ignore", TqdmWarning)

        progress_bar = tqdm(
            total=total_length,
            desc=desc,
            bar_format="{desc}: {percentage:.0f}%|{bar}| {n:.0f}/{total_fmt} [{elapsed}<{remaining}",
        )
        return progress_bar

    def map_with_progressbar(
        self, func, iterable, total_length: int, desc: Optional[str] = None
    ):
        import threading
        from threading import Thread

        progress_bar = self.get_progressbar(total_length, desc)
        progress_queue = self.create_queue()

        def sub_func(*args, **kwargs):
            def pg_callback(value: int):
                progress_queue.put(value)  # type: ignore[trust]
                pass

            return func(pg_callback, *args, **kwargs)

        def update_pg(bar, queue):
            while True:
                r = queue.get()
                if r is not None:
                    bar.update(r)
                else:
                    break

        progress_thread: Thread = threading.Thread(
            target=update_pg, args=(progress_bar, progress_queue)
        )
        progress_thread.start()

        try:
            result = self.map(sub_func, iterable)
        finally:
            progress_queue.put(None)  # type: ignore[trust]
            progress_thread.join()
            progress_bar.close()

            if hasattr(progress_queue, "close"):
                progress_bar.close()

        return result

    @abstractmethod
    def create_queue(self):
        """Creates queue for specific provider"""

    @abstractmethod
    def map(self, func, iterable):
        """Applies 'func' to each element in 'iterable', collecting the results
        in a list that is returned.
        """

    @abstractmethod
    def close(self):
        """Closes the provider."""
