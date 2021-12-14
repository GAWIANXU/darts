"""
Scaler
------
"""

from typing import Sequence, Iterator, Any, Tuple
from darts.dataprocessing.transformers import InvertibleDataTransformer, FittableDataTransformer
from darts.timeseries import TimeSeries
from darts.logging import get_logger, raise_log
from sklearn.preprocessing import MinMaxScaler
from copy import deepcopy

logger = get_logger(__name__)


class Scaler(InvertibleDataTransformer, FittableDataTransformer):
    def __init__(self,
                 scaler=None,
                 name="Scaler",
                 n_jobs: int = 1,
                 verbose: bool = False):
        """
        Generic wrapper class for using scalers that implement `fit()`, `transform()` and
        `inverse_transform()` methods (typically from scikit-learn) on `TimeSeries`.

        When the scaler is applied on multivariate series, the scaling is done per-component.
        When the series are stochastic, the scaling is done across all samples (for each given component).
        The transformation is applied independently for each dimension (component) of the time series,
        effectively merging all samples of a component in order to compute the transform.

        Parameters
        ----------
        scaler
            The scaler to transform the data with. It must provide `fit()`, `transform()` and `inverse_transform()`
            methods.
            Default: `sklearn.preprocessing.MinMaxScaler(feature_range=(0, 1))`; this will scale all the values
            of a time series between 0 and 1.
            In case the `Scaler` is applied to multiple `TimeSeries` objects, a deep-copy of the chosen scaler
            will be instantiated, fitted, and stored, for each `TimeSeries`.
        name
            A specific name for the scaler
        n_jobs
            The number of jobs to run in parallel. Parallel jobs are created only when a `Sequence[TimeSeries]` is
            passed as input to a method, parallelising operations regarding different `TimeSeries`. Defaults to `1`
            (sequential). Setting the parameter to `-1` means using all the available processors.
            Note: for a small amount of data, the parallelisation overhead could end up increasing the total
            required amount of time.
        verbose
            Optionally, whether to print operations progress
        """

        super().__init__(name=name, n_jobs=n_jobs, verbose=verbose)

        if scaler is None:
            scaler = MinMaxScaler(feature_range=(0, 1))

        if (not callable(getattr(scaler, "fit", None)) or not callable(getattr(scaler, "transform", None))
                or not callable(getattr(scaler, "inverse_transform", None))):  # noqa W503
            raise_log(ValueError(
                      'The provided transformer object must have fit(), transform() and inverse_transform() methods'),
                      logger)

        self.transformer = scaler
        self.transformer_instances = None

    @staticmethod
    def ts_transform(series: TimeSeries, transformer, **kwargs) -> TimeSeries:
        component_mask = kwargs.get('component_mask', None)

        tr_out = transformer.transform(Scaler._reshape_in(series, component_mask=component_mask))

        transformed_vals = Scaler._reshape_out(series, tr_out, component_mask=component_mask)

        return TimeSeries.from_times_and_values(times=series.time_index,
                                                values=transformed_vals,
                                                fill_missing_dates=False,
                                                columns=series.columns)

    @staticmethod
    def ts_inverse_transform(series: TimeSeries, transformer, *args, **kwargs) -> TimeSeries:
        component_mask = kwargs.get('component_mask', None)

        tr_out = transformer.inverse_transform(Scaler._reshape_in(series, component_mask=component_mask))
        inv_transformed_vals = Scaler._reshape_out(series, tr_out, component_mask=component_mask)

        return TimeSeries.from_times_and_values(times=series.time_index,
                                                values=inv_transformed_vals,
                                                fill_missing_dates=False,
                                                columns=series.columns)

    @staticmethod
    def ts_fit(series: TimeSeries, transformer, *args, **kwargs) -> Any:
        # fit_parameter will receive the transformer object instance
        component_mask = kwargs.get('component_mask', None)

        scaler = transformer.fit(Scaler._reshape_in(series, component_mask=component_mask))
        return scaler

    def _fit_iterator(self, series: Sequence[TimeSeries]) -> Iterator[Tuple[TimeSeries, Any]]:
        # generator which returns deep copies of the 'scaler' argument
        new_scaler_gen = (deepcopy(self.transformer) for _ in range(len(series)))
        return zip(series, new_scaler_gen)

    def _transform_iterator(self, series: Sequence[TimeSeries]) -> Iterator[Tuple[TimeSeries, Any]]:
        # since '_ts_fit()' returns the scaler objects, the 'fit()' call will save transformers instance into
        # self._fitted_params
        return zip(series, self._fitted_params)

    def _inverse_transform_iterator(self, series: Sequence[TimeSeries]) -> Iterator[Tuple[TimeSeries, Any]]:
        # the same self._fitted_params will be used also for the 'ts_inverse_transform()'
        return zip(series, self._fitted_params)