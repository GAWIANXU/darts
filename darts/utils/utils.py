"""
Additional util functions
-------------------------
"""
from enum import Enum
from functools import wraps
from inspect import Parameter, getcallargs, signature
from types import SimpleNamespace
from typing import Callable, Iterator, List, Optional, Sequence, Tuple, TypeVar, Union

import numpy as np
import pandas as pd
from joblib import Parallel, delayed
from tqdm import tqdm
from tqdm.notebook import tqdm as tqdm_notebook

from darts import TimeSeries
from darts.logging import get_logger, raise_if_not, raise_log
from darts.utils.timeseries_generation import generate_index

try:
    from IPython import get_ipython
except ModuleNotFoundError:
    get_ipython = None

logger = get_logger(__name__)


# Enums
class SeasonalityMode(Enum):
    MULTIPLICATIVE = "multiplicative"
    ADDITIVE = "additive"
    NONE = None


class TrendMode(Enum):
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


class ModelMode(Enum):
    MULTIPLICATIVE = "multiplicative"
    ADDITIVE = "additive"
    NONE = None


# TODO: we do not check the time index here
def retain_period_common_to_all(series: List[TimeSeries]) -> List[TimeSeries]:
    """
    Trims all series in the provided list, if necessary, so that the returned time series have
    a common span (corresponding to largest time sub-interval common to all series).

    Parameters
    ----------
    series
        The list of series to consider.

    Raises
    ------
    ValueError
        If no common time sub-interval exists

    Returns
    -------
    List[TimeSeries]
        A list of series, where each series have the same span
    """

    last_first = max(map(lambda s: s.start_time(), series))
    first_last = min(map(lambda s: s.end_time(), series))

    if last_first >= first_last:
        raise_log(
            ValueError("The provided time series must have nonzero overlap"), logger
        )

    return list(map(lambda s: s.slice(last_first, first_last), series))


def _build_tqdm_iterator(iterable, verbose, **kwargs):
    """
    Build an iterable, possibly using tqdm (either in notebook or regular mode)

    Parameters
    ----------
    iterable
    verbose
    total
        Length of the iterator, helps in cases where tqdm is not detecting the total length.

    Returns
    -------
    """

    def _isnotebook():
        if get_ipython is None:
            return False
        try:
            shell = get_ipython().__class__.__name__
            if shell == "ZMQInteractiveShell":
                return True  # Jupyter notebook or qtconsole
            elif shell == "TerminalInteractiveShell":
                return False  # Terminal running IPython
            else:
                return False  # Other type (?)
        except NameError:
            return False  # Probably standard Python interpreter

    if verbose:
        if _isnotebook():
            iterator = tqdm_notebook(iterable, **kwargs)
        else:
            iterator = tqdm(iterable, **kwargs)

    else:
        iterator = iterable
    return iterator


# Types for sanity checks decorator
A = TypeVar("A")
B = TypeVar("B")
T = TypeVar("T")


def _with_sanity_checks(
    *sanity_check_methods: str,
) -> Callable[[Callable[[A, B], T]], Callable[[A, B], T]]:
    """
    Decorator allowing to specify some sanity check method(s) to be used on a class method.
    The decorator guarantees that args and kwargs from the method to sanitize will be available in the
    sanity check methods as specified in the sanitized method's signature, irrespective of how it was called.

    Parameters
    ----------
    *sanity_check_methods
        one or more sanity check methods that will be called with all the parameter of the decorated method.

    Returns
    -------
    A Callable corresponding to the decorated method.

    Examples
    --------
    class Model:
        def _a_sanity_check(self, *args, **kwargs):
            raise_if_not(kwargs['b'] == kwargs['c'], 'b must equal c', logger)
        @_with_sanity_checks("_a_sanity_check")
        def fit(self, a, b=0, c=0):
            # at this point we can safely assume that 'b' and 'c' are equal...
            ...
    """

    def decorator(method_to_sanitize: Callable[[A, B], T]) -> Callable[[A, B], T]:
        @wraps(method_to_sanitize)
        def sanitized_method(self, *args: A, **kwargs: B) -> T:
            for sanity_check_method in sanity_check_methods:
                # Convert all arguments into keyword arguments
                all_as_kwargs = getcallargs(method_to_sanitize, self, *args, **kwargs)

                # Then separate args from kwargs according to the function's signature
                only_args = all_as_kwargs.copy()
                only_kwargs = all_as_kwargs.copy()

                for param_name, param in signature(
                    method_to_sanitize
                ).parameters.items():
                    if (
                        param.default == Parameter.empty
                        and param.kind != Parameter.VAR_POSITIONAL
                    ):
                        only_kwargs.pop(param_name)
                    else:
                        only_args.pop(param_name)

                only_args.pop("self")

                getattr(self, sanity_check_method)(*only_args.values(), **only_kwargs)
            return method_to_sanitize(self, *only_args.values(), **only_kwargs)

        return sanitized_method

    return decorator


def _historical_forecasts_general_checks(model, series, kwargs):
    """
    Performs checks common to ForecastingModel and RegressionModel backtest() methods
    Parameters
    ----------
    model
        The forecasting model.
    series
        Either series when called from ForecastingModel, or target_series if called from RegressionModel
    signature_params
        A dictionary of the signature parameters of the calling method, to get the default values
        Typically would be signature(self.backtest).parameters
    kwargs
        Params specified by the caller of backtest(), they take precedence over the arguments' default values
    """
    # parse kwargs
    n = SimpleNamespace(**kwargs)

    # check forecast horizon
    raise_if_not(
        n.forecast_horizon > 0,
        "The provided forecasting horizon must be a positive integer.",
        logger,
    )

    # check stride
    raise_if_not(
        n.stride > 0,
        "The provided stride parameter must be a positive integer.",
        logger,
    )

    series = series2seq(series)

    if n.start is not None:
        # check start parameter in general (non series dependent)
        if not isinstance(n.start, (float, int, np.int64, pd.Timestamp)):
            raise_log(
                TypeError(
                    "`start` needs to be either `float`, `int`, `pd.Timestamp` or `None`"
                ),
                logger,
            )
        if isinstance(n.start, float):
            raise_if_not(
                0.0 <= n.start <= 1.0, "`start` should be between 0.0 and 1.0.", logger
            )
        elif isinstance(n.start, (int, np.int64)):
            raise_if_not(
                n.start >= 0, "if `start` is an integer, must be `>= 0`.", logger
            )

        # verbose error messages
        if not isinstance(n.start, pd.Timestamp):
            start_value_msg = f"`start` value `{n.start}` corresponding to timestamp"
        else:
            start_value_msg = "`start` time"
        for idx, series_ in enumerate(series):
            # check specifically for int and Timestamp as error by `get_timestamp_at_point` is too generic
            if isinstance(n.start, pd.Timestamp):
                if n.start > series_.end_time():
                    raise_log(
                        ValueError(
                            f"`start` time `{n.start}` is after the last timestamp `{series_.end_time()}` of the "
                            f"series at index: {idx}."
                        ),
                        logger,
                    )
                elif n.start < series_.start_time():
                    raise_log(
                        ValueError(
                            f"`start` time `{n.start}` is before the first timestamp `{series_.start_time()}` of the "
                            f"series at index: {idx}."
                        ),
                        logger,
                    )
            elif isinstance(n.start, (int, np.int64)):
                if (
                    series_.has_datetime_index
                    or (series_.has_range_index and series_.freq == 1)
                ) and n.start >= len(series_):
                    raise_log(
                        ValueError(
                            f"`start` index `{n.start}` is out of bounds for series of length {len(series_)} "
                            f"at index: {idx}."
                        ),
                        logger,
                    )
                elif (
                    series_.has_range_index and series_.freq > 1
                ) and n.start > series_.time_index[-1]:
                    raise_log(
                        ValueError(
                            f"`start` index `{n.start}` is larger than the last index `{series_.time_index[-1]}` "
                            f"for series at index: {idx}."
                        ),
                        logger,
                    )

            start = series_.get_timestamp_at_point(n.start)
            if n.retrain is not False and start == series_.start_time():
                raise_log(
                    ValueError(
                        f"{start_value_msg} `{start}` is the first timestamp of the series {idx}, resulting in an "
                        f"empty training set."
                    ),
                    logger,
                )

            # check that overlap_end and start together form a valid combination
            overlap_end = n.overlap_end
            if not overlap_end and not (
                start + (series_.freq * (n.forecast_horizon - 1)) in series_
            ):
                raise_log(
                    ValueError(
                        f"{start_value_msg} `{start}` is too late in the series {idx} to make any predictions with "
                        f"`overlap_end` set to `False`."
                    ),
                    logger,
                )

    # check direct likelihood parameter prediction before fitting a model
    if n.predict_likelihood_parameters:
        if not model.supports_likelihood_parameter_prediction:
            raise_log(
                ValueError(
                    f"Model `{model.__class__.__name__}` does not support `predict_likelihood_parameters=True`. "
                    f"Either the model does not support likelihoods, or no `likelihood` was used at model "
                    f"creation."
                )
            )
        if n.num_samples != 1:
            raise_log(
                ValueError(
                    f"`predict_likelihood_parameters=True` is only supported for `num_samples=1`, "
                    f"received {n.num_samples}."
                ),
                logger,
            )

        if (
            model.output_chunk_length is not None
            and n.forecast_horizon > model.output_chunk_length
        ):
            raise_log(
                ValueError(
                    "`predict_likelihood_parameters=True` is only supported for `forecast_horizon` smaller than or "
                    "equal to model's `output_chunk_length`."
                ),
                logger,
            )


def _parallel_apply(
    iterator: Iterator[Tuple], fn: Callable, n_jobs: int, fn_args, fn_kwargs
) -> List:
    """
    Utility function that parallelise the execution of a function over an Iterator

    Parameters
    ----------
    iterator (Iterator[Tuple])
        Iterator which returns tuples of input value to feed to fn. Constant `args` and `kwargs` should passed through
        `fn_args` and  `fn_kwargs` respectively.
    fn (Callable)
        The function to be parallelized.
    n_jobs (int)
        The number of jobs to run in parallel. Defaults to `1` (sequential). Setting the parameter to `-1` means using
        all the available processors.
        Note: for a small amount of data, the parallelisation overhead could end up increasing the total
        required amount of time.
    fn_args
        Additional arguments for each `fn()` call
    fn_kwargs
        Additional keyword arguments for each `fn()` call

    """

    returned_data = Parallel(n_jobs=n_jobs)(
        delayed(fn)(*sample, *fn_args, **fn_kwargs) for sample in iterator
    )
    return returned_data


def _check_quantiles(quantiles):
    raise_if_not(
        all([0 < q < 1 for q in quantiles]),
        "All provided quantiles must be between 0 and 1.",
    )

    # we require the median to be present and the quantiles to be symmetric around it,
    # for correctness of sampling.
    median_q = 0.5
    raise_if_not(
        median_q in quantiles, "median quantile `q=0.5` must be in `quantiles`"
    )
    is_centered = [
        -1e-6 < (median_q - left_q) + (median_q - right_q) < 1e-6
        for left_q, right_q in zip(quantiles, quantiles[::-1])
    ]
    raise_if_not(
        all(is_centered),
        "quantiles lower than `q=0.5` need to share same difference to `0.5` as quantiles "
        "higher than `q=0.5`",
    )


def series2seq(
    ts: Optional[Union[TimeSeries, Sequence[TimeSeries]]]
) -> Optional[Sequence[TimeSeries]]:
    """If `ts` is a single TimeSeries, return it as a list of a single TimeSeries.

    Parameters
    ----------
    ts
        None, a single TimeSeries, or a sequence of TimeSeries

    Returns
    -------
        `ts` if `ts` is not a TimeSeries, else `[ts]`

    """
    return [ts] if isinstance(ts, TimeSeries) else ts


def seq2series(
    ts: Optional[Union[TimeSeries, Sequence[TimeSeries]]]
) -> Optional[TimeSeries]:
    """If `ts` is a Sequence with only a single series, return the single series as TimeSeries.

    Parameters
    ----------
    ts
        None, a single TimeSeries, or a sequence of TimeSeries

    Returns
    -------
        `ts` if `ts` if is not a single element TimeSeries sequence, else `ts[0]`

    """

    return ts[0] if isinstance(ts, Sequence) and len(ts) == 1 else ts


def slice_index(
    index: Union[pd.RangeIndex, pd.DatetimeIndex],
    start: Union[int, pd.Timestamp],
    end: Union[int, pd.Timestamp],
) -> Union[pd.RangeIndex, pd.DatetimeIndex]:
    """
    Returns a new Index with the same type as the input `index`, containing the values between `start`
    and `end` included. If start and end are not in the index, the closest values are used instead.
    The start and end values can be either integers (in which case they are interpreted as indices),
    or pd.Timestamps (in which case they are interpreted as actual timestamps).


    Parameters
    ----------
    index
        The index to slice.
    start
        The start of the returned index.
    end
        The end of the returned index.

    Returns
    -------
    Union[pd.RangeIndex, pd.DatetimeIndex]
        A new index with the same type as the input `index`, but with only the values between `start` and `end`
        included.
    """

    if type(start) != type(end):
        raise_log(
            ValueError(
                "start and end values must be of the same type (either both integers or both pd.Timestamps)"
            ),
            logger,
        )

    if isinstance(start, pd.Timestamp) and isinstance(index, pd.RangeIndex):
        raise_log(
            ValueError(
                "start and end values are a pd.Timestamp, but time_index is a RangeIndex. "
                "Please provide an integer start value."
            ),
            logger,
        )
    if isinstance(start, int) and isinstance(index, pd.DatetimeIndex):
        raise_log(
            ValueError(
                "start and end value are integer, but time_index is a RangeIndex. "
                "Please provide an integer end value."
            ),
            logger,
        )

    start_idx = index.get_indexer(generate_index(start, length=1), method="nearest")[0]
    end_idx = index.get_indexer(generate_index(end, length=1), method="nearest")[0]

    return index[start_idx : end_idx + 1]


def drop_before_index(
    index: Union[pd.RangeIndex, pd.DatetimeIndex],
    split_point: Union[int, pd.Timestamp],
) -> Union[pd.RangeIndex, pd.DatetimeIndex]:
    """
    Drops everything before the provided time `split_point` (excluded) from the index.

    Parameters
    ----------
    index
        The index to drop values from.
    split_point
        The timestamp that indicates cut-off time.

    Returns
    -------
    Union[pd.RangeIndex, pd.DatetimeIndex]
        A new index with values before `split_point` dropped.
    """
    return slice_index(index, split_point, index[-1])


def drop_after_index(
    index: Union[pd.RangeIndex, pd.DatetimeIndex],
    split_point: Union[int, pd.Timestamp],
) -> Union[pd.RangeIndex, pd.DatetimeIndex]:
    """
    Drops everything after the provided time `split_point` (excluded) from the index.

    Parameters
    ----------
    index
        The index to drop values from.
    split_point
        The timestamp that indicates cut-off time.

    Returns
    -------
    Union[pd.RangeIndex, pd.DatetimeIndex]
        A new index with values after `split_point` dropped.
    """

    return slice_index(index, index[0], split_point)


def get_single_series(
    ts: Optional[Union[TimeSeries, Sequence[TimeSeries]]]
) -> Optional[TimeSeries]:
    """Returns a single (first) TimeSeries or `None` from `ts`. Returns `ts` if  `ts` is a TimeSeries, `ts[0]` if
    `ts` is a Sequence of TimeSeries. Otherwise, returns `None`.

    Parameters
    ----------
    ts
        None, a single TimeSeries, or a sequence of TimeSeries.

    Returns
    -------
        `ts` if  `ts` is a TimeSeries, `ts[0]` if `ts` is a Sequence of TimeSeries. Otherwise, returns `None`

    """
    if isinstance(ts, TimeSeries) or ts is None:
        return ts
    else:
        return ts[0]
