# TODO: remove this in the future (PEP deferred for 3.11, now 3.12?)
from __future__ import annotations

import glob
import logging
import os
from copy import copy
from typing import TYPE_CHECKING

import dask.array as da
import numpy as np
import xarray as xr
import zarr
from tifffile import TiffFile

from iohub.reader_base import ReaderBase

if TYPE_CHECKING:
    from _typeshed import StrOrBytesPath


class MMStack:
    """Micro-Manager multi-file OME-TIFF (MMStack) reader.

    Parameters
    ----------
    data_path : StrOrBytesPath
        Path to the directory containing OME-TIFF files
        or the path to the first OME-TIFF file in the series
    dask_item : bool, optional
        Whether to return dask arrays instead of xarray from ``__getitem__()``,
        by default False
    """

    def __init__(self, data_path: StrOrBytesPath, dask_item: bool = False):
        super().__init__()
        data_path = str(data_path)
        if os.path.isfile(data_path):
            if "ome.tif" in os.path.basename(data_path):
                first_file = data_path
            else:
                raise ValueError("{data_path} is not a OME-TIFF file.")
        elif os.path.isdir(data_path):
            files = glob.glob(os.path.join(data_path, "*.ome.tif"))
            if not files:
                raise FileNotFoundError(
                    f"Path {data_path} contains no OME-TIFF files, "
                )
            else:
                first_file = files[0]
        self.dirname = os.path.basename(os.path.dirname(first_file))
        self._first_tif = TiffFile(first_file, is_mmstack=True)
        self._parse_data()
        self._store = None
        self._asdask = dask_item

    def _parse_data(self):
        series = self._first_tif.series[0]
        raw_dims = dict(
            (axis, size)
            for axis, size in zip(series.get_axes(), series.get_shape())
        )
        axes = ("R", "T", "C", "Z", "Y", "X")
        dims = dict((ax, raw_dims.get(ax) or 1) for ax in axes)
        logging.debug(f"Got dataset dimensions from tifffile: {dims}.")
        (
            self.positions,
            self.frames,
            self.channels,
            self.slices,
            self.height,
            self.width,
        ) = dims.values()
        self._store = series.aszarr()
        logging.debug(f"Opened {self._store}.")
        data = da.from_zarr(zarr.open(self._store))
        img = xr.DataArray(data, dims=raw_dims, name=self.dirname)
        self._xdata = img.expand_dims(
            [ax for ax in axes if ax not in img.dims]
        ).transpose(*axes)

    @property
    def xdata(self):
        return self._xdata

    def __len__(self):
        return self.positions

    def __getitem__(self, key: int):
        item = self.xdata.sel(R=key)
        return item.data if self._asdask else item

    def __setitem__(self, key, value):
        raise PermissionError("MMStack is read-only.")

    def __delitem__(self, key, value):
        raise PermissionError("MMStack is read-only.")

    def __contains__(self, key):
        return key in self.xdata.R

    def __iter__(self):
        for key in self.xdata.R:
            yield self[key]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self._first_tif.close()


class MicromanagerOmeTiffReader(ReaderBase):
    def __init__(self, folder: str, extract_data: bool = False):
        super().__init__()

        """
        Parameters
        ----------
        folder:         (str)
            folder or file containing all ome-tiff files
        extract_data:   (bool)
            True if ome_series should be extracted immediately

        """

        # Add Initial Checks
        if len(glob.glob(os.path.join(folder, "*.ome.tif"))) == 0:
            raise ValueError(
                (
                    f"Path {folder} contains no `.ome.tif` files, "
                    "please specify a valid input directory."
                )
            )

        # Grab all image files
        self.data_directory = folder
        self._files = glob.glob(os.path.join(self.data_directory, "*.ome.tif"))

        # Generate Data Specific Properties
        self.coords = None
        self.coord_map = dict()
        self.pos_names = []
        self.position_arrays = dict()
        self.positions = 0
        self.frames = 0
        self.channels = 0
        self.slices = 0
        self.height = 0
        self.width = 0
        self._set_dtype()

        # Initialize MM attributes
        self.z_step_size = None
        self.channel_names = []

        # Read MM data
        self._set_mm_meta()

        # Gather index map of file, page, byte offset
        self._gather_index_maps()

        # if extract data, create all of the virtual zarr stores up front
        if extract_data:
            for i in range(self.positions):
                self._create_position_array(i)

    def _gather_index_maps(self):
        """
        Will return a dictionary of {coord: (filepath, page, byte_offset)}
        of length (N_Images) to later query

        Returns
        -------

        """

        positions = 0
        frames = 0
        channels = 0
        slices = 0
        for file in self._files:
            tf = TiffFile(file)
            meta = tf.micromanager_metadata["IndexMap"]
            tf.close()
            offsets = self._get_byte_offsets(meta)
            for page, offset in enumerate(offsets):
                coord = [0, 0, 0, 0]
                coord[0] = meta["Position"][page]
                coord[1] = meta["Frame"][page]
                coord[2] = meta["Channel"][page]
                coord[3] = meta["Slice"][page]
                self.coord_map[tuple(coord)] = (file, page, offset)

                # update dimensions as we go along,
                # helps with incomplete datasets
                if coord[0] + 1 > positions:
                    positions = coord[0] + 1

                if coord[1] + 1 > frames:
                    frames = coord[1] + 1

                if coord[2] + 1 > channels:
                    channels = coord[2] + 1

                if coord[3] + 1 > slices:
                    slices = coord[3] + 1

        # update dimensions to the largest dimensions present in the saved data
        self.positions = positions
        self.frames = frames
        self.channels = channels
        self.slices = slices

    @staticmethod
    def _get_byte_offsets(meta: dict):
        """Get byte offsets from Micro-Manager metadata.

        Parameters
        ----------
        meta : dict
            Micro-Manager metadata in the OME-TIFF header

        Returns
        -------
        list
            List of byte offsets for image arrays in the multi-page TIFF file
        """
        offsets = meta["Offset"][meta["Offset"] > 0]
        offsets[0] += 210  # first page array offset
        offsets[1:] += 162  # image array offset
        return list(offsets)

    def _set_mm_meta(self):
        """
        assign image metadata from summary metadata

        Returns
        -------

        """
        with TiffFile(self._files[0]) as tif:
            self.mm_meta = tif.micromanager_metadata

            mm_version = self.mm_meta["Summary"]["MicroManagerVersion"]
            if "beta" in mm_version:
                if self.mm_meta["Summary"]["Positions"] > 1:
                    self.stage_positions = []

                    for p in range(
                        len(self.mm_meta["Summary"]["StagePositions"])
                    ):
                        pos = self._simplify_stage_position_beta(
                            self.mm_meta["Summary"]["StagePositions"][p]
                        )
                        self.stage_positions.append(pos)

                # MM beta versions sometimes don't have 'ChNames',
                # so I'm wrapping in a try-except and setting the
                # channel names to empty strings if it fails.
                try:
                    for ch in self.mm_meta["Summary"]["ChNames"]:
                        self.channel_names.append(ch)
                except Exception:
                    self.channel_names = self.mm_meta["Summary"][
                        "Channels"
                    ] * [
                        ""
                    ]  # empty strings

            elif mm_version == "1.4.22":
                for ch in self.mm_meta["Summary"]["ChNames"]:
                    self.channel_names.append(ch)

            else:
                if self.mm_meta["Summary"]["Positions"] > 1:
                    self.stage_positions = []

                    for p in range(self.mm_meta["Summary"]["Positions"]):
                        pos = self._simplify_stage_position(
                            self.mm_meta["Summary"]["StagePositions"][p]
                        )
                        self.stage_positions.append(pos)

                for ch in self.mm_meta["Summary"]["ChNames"]:
                    self.channel_names.append(ch)

            # dimensions based on mm metadata
            # do not reflect final written dimensions
            # these will change after data is loaded
            self.z_step_size = self.mm_meta["Summary"]["z-step_um"]
            self.height = self.mm_meta["Summary"]["Height"]
            self.width = self.mm_meta["Summary"]["Width"]
            self.frames = self.mm_meta["Summary"]["Frames"]
            self.slices = self.mm_meta["Summary"]["Slices"]
            self.channels = self.mm_meta["Summary"]["Channels"]

    def _simplify_stage_position(self, stage_pos: dict):
        """
        flattens the nested dictionary structure of stage_pos
        and removes superfluous keys

        Parameters
        ----------
        stage_pos:      (dict)
            dictionary containing a single position's device info

        Returns
        -------
        out:            (dict)
            flattened dictionary
        """

        out = copy(stage_pos)
        out.pop("DevicePositions")
        for dev_pos in stage_pos["DevicePositions"]:
            out.update({dev_pos["Device"]: dev_pos["Position_um"]})
        return out

    def _simplify_stage_position_beta(self, stage_pos: dict):
        """
        flattens the nested dictionary structure of stage_pos
        and removes superfluous keys
        for MM2.0 Beta versions

        Parameters
        ----------
        stage_pos:      (dict)
            dictionary containing a single position's device info

        Returns
        -------
        new_dict:       (dict)
            flattened dictionary

        """

        new_dict = {}
        new_dict["Label"] = stage_pos["label"]
        new_dict["GridRow"] = stage_pos["gridRow"]
        new_dict["GridCol"] = stage_pos["gridCol"]

        for sub in stage_pos["subpositions"]:
            values = []
            for field in ["x", "y", "z"]:
                if sub[field] != 0:
                    values.append(sub[field])
            if len(values) == 1:
                new_dict[sub["stageName"]] = values[0]
            else:
                new_dict[sub["stageName"]] = values

        return new_dict

    def _create_position_array(self, pos):
        """maps all of the tiff data into a virtual zarr store
        in memory for a given position

        Parameters
        ----------
        pos:            (int) index of the position to create array under

        Returns
        -------

        """

        # intialize virtual zarr store and save it under positions
        timepoints, channels, slices = self._get_dimensions(pos)
        self.position_arrays[pos] = zarr.zeros(
            shape=(timepoints, channels, slices, self.height, self.width),
            chunks=(1, 1, 1, self.height, self.width),
            dtype=self.dtype,
        )
        # add all the images with this specific dimension.
        # Will be blank images if dataset
        # is incomplete
        for p, t, c, z in self.coord_map.keys():
            if p == pos:
                self.position_arrays[pos][t, c, z, :, :] = self.get_image(
                    pos, t, c, z
                )

    def _set_dtype(self):
        """
        gets the datatype from any image plane metadata

        Returns
        -------

        """

        tf = TiffFile(self._files[0])

        self.dtype = tf.pages[0].dtype
        tf.close()

    def _get_dimensions(self, position):
        """
        Gets the max dimensions from the current position
        in case of incomplete datasets

        Parameters
        ----------
        position:       (int) Position index to grab dimensions from

        Returns
        -------

        """

        t = 0
        c = 0
        z = 0

        # dimension size = index + 1
        for tup in self.coord_map.keys():
            if position != tup[0]:
                continue
            else:
                if tup[1] + 1 > t:
                    t = tup[1] + 1
                if tup[2] + 1 > c:
                    c = tup[2] + 1
                if tup[3] + 1 > z:
                    z = tup[3] + 1

        return t, c, z

    def get_image(self, p, t, c, z):
        """
        get the image at a specific coordinate through memory mapping

        Parameters
        ----------
        p:              (int) position index
        t:              (int) time index
        c:              (int) channel index
        z:              (int) slice/z index

        Returns
        -------
        image:          (np-array)
            numpy array of shape (Y, X) at given coordinate

        """

        coord_key = (p, t, c, z)
        coord = self.coord_map[coord_key]  # (file, page, offset)

        return np.memmap(
            coord[0],
            dtype=self.dtype,
            mode="r",
            offset=coord[2],
            shape=(self.height, self.width),
        )

    def get_zarr(self, position):
        """
        return a zarr array for a given position

        Parameters
        ----------
        position:       (int) position (aka ome-tiff scene)

        Returns
        -------
        position:       (zarr.array)

        """
        if position not in self.position_arrays.keys():
            self._create_position_array(position)
        return self.position_arrays[position]

    def get_array(self, position):
        """
        return a numpy array for a given position

        Parameters
        ----------
        position:   (int) position (aka ome-tiff scene)

        Returns
        -------
        position:   (np.ndarray)

        """

        # if position hasn't been initialized in memory, do that.
        if position not in self.position_arrays.keys():
            self._create_position_array(position)

        return np.array(self.position_arrays[position])

    def get_num_positions(self):
        """
        get total number of scenes referenced in ome-tiff metadata

        Returns
        -------
        number of positions     (int)

        """
        return self.positions
