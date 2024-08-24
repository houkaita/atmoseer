from datetime import timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import numpy.typing as npt
import pandas as pd
import xarray as xr
from pydantic import BaseModel

from .get_neighbors import get_bottom_neighbor, get_right_neighbor, get_upper_neighbor
from .WebSirenesKeys import WebSirenesKeys, websirenes_keys


class Square(BaseModel):
    top_left: tuple[float, float]
    bottom_left: tuple[float, float]
    bottom_right: tuple[float, float]
    top_right: tuple[float, float]


class WebSirenesSquare:
    def __init__(self, websirenes_keys: WebSirenesKeys) -> None:
        self.websirenes_keys = websirenes_keys

    def get_keys_in_square(self, square: Square) -> list[str]:
        """
        Get the keys of the websirenes datasets that are inside the square
        Args:
            square (Square): The square to check for keys
        """
        keys = [x.stem for x in Path(self.websirenes_keys.websirenes_keys_path).glob("*.parquet")]
        websirenes_keys = []
        for key in keys:
            key_lat, key_lon = map(float, key.split("_"))

            if key_lat < square.bottom_left[0] or key_lat > square.top_left[0]:
                continue
            if key_lon < square.top_left[1] or key_lon > square.top_right[1]:
                continue
            websirenes_keys.append(key)
        return websirenes_keys

    def _get_era5land_precipitation_in_square(
        self, square: Square, era5land_at_time: xr.Dataset
    ) -> float:
        tl_lat, tl_lon = square.top_left
        bl_lat, bl_lon = square.bottom_left
        br_lat, br_lon = square.bottom_right
        tr_lat, tr_lon = square.top_right

        top_left = era5land_at_time.sel(latitude=tl_lat, longitude=tl_lon)
        bottom_left = era5land_at_time.sel(latitude=bl_lat, longitude=bl_lon)
        bottom_right = era5land_at_time.sel(latitude=br_lat, longitude=br_lon)
        top_right = era5land_at_time.sel(latitude=tr_lat, longitude=tr_lon)

        assert top_left["tp"].size == 1
        assert bottom_left["tp"].size == 1
        assert bottom_right["tp"].size == 1
        assert top_right["tp"].size == 1

        top_left = top_left["tp"].item()
        bottom_left = bottom_left["tp"].item()
        bottom_right = bottom_right["tp"].item()
        top_right = top_right["tp"].item()

        max_tp: float = max(
            top_left,
            bottom_left,
            bottom_right,
            top_right,
        )

        if np.isnan(max_tp):
            # If max_tp is NaN between ERA5Land gridpoints, it means that we are out of land in the square
            # For now, we are returning 0, but we should consider a better approach. For example, using the nearest land gridpoint
            return 0.0
        return max_tp

    def get_precipitation_in_square(
        self,
        square: Square,
        websirenes_keys: list[str],
        timestamp: pd.Timestamp,
        ds_time: xr.Dataset,
    ) -> float:
        if len(websirenes_keys) == 0:
            # No stations in the square, use ERA5Land precipitation in square
            return self._get_era5land_precipitation_in_square(square, ds_time)

        precipitations_15_min_aggregated: list[float] = []
        for key in websirenes_keys:
            df_web = self.websirenes_keys.load_key(key)

            time_upper_bound = timestamp
            time_lower_bound = timestamp - timedelta(minutes=45)

            df_web_filtered = df_web[
                (df_web.index >= time_lower_bound) & (df_web.index <= time_upper_bound)
            ]

            m15 = df_web_filtered["m15"]

            if m15.isnull().all():
                # All values are NaN in station "key" from "time_lower_bound" to "time_upper_bound",
                # use ERA5Land max precipitation in square
                m15 = np.array(self._get_era5land_precipitation_in_square(square, ds_time))
            precipitations_15_min_aggregated.append(m15.sum().item())
        max_precipitation = max(precipitations_15_min_aggregated)
        # see "ge=0, but txt has -99.99 values"
        if max_precipitation < 0:
            return 0.0
        return max_precipitation

    def get_square(
        self,
        lat: float,
        lon: float,
        sorted_latitudes_ascending: npt.NDArray[np.float32],
        sorted_longitudes_ascending: npt.NDArray[np.float32],
    ) -> Optional[Square]:
        """
        Get the square that contains the point (lat, lon)
        Example, given this grid:
              0   1   2   3
            0 *   *   *   *
            1 *   *   *   *
            2 *   *   *   *
        Given that lat long "*" is the top_left = (0,0):
        top_left's bottom_left neighbor = (1,0)
        bottom_left's bottom_right neighbor = (1,1)
        bottom_right's top_right neighbor = (0,1)

        With top_left, bottom_left, bottom_right, top_right we can create a square

        Note we can get out of bounds, that's when we return None.
        For example, there's no bottom neighbor for (3,3)
        """
        bottom_neighbor = get_bottom_neighbor(lat, lon, sorted_latitudes_ascending)
        if bottom_neighbor is None:
            return None
        lat_bottom, lon_bottom = bottom_neighbor

        right_neighbor = get_right_neighbor(lat_bottom, lon_bottom, sorted_longitudes_ascending)
        if right_neighbor is None:
            return None
        lat_right, lon_right = right_neighbor

        upper_neighbor = get_upper_neighbor(lat_right, lon_right, sorted_latitudes_ascending)
        if upper_neighbor is None:
            return None
        lat_upper, lon_upper = upper_neighbor

        return Square(
            top_left=(lat, lon),
            bottom_left=(lat_bottom, lon_bottom),
            bottom_right=(lat_right, lon_right),
            top_right=(lat_upper, lon_upper),
        )


websirenes_square = WebSirenesSquare(websirenes_keys)
