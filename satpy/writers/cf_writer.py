#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2017-2019 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.
"""Writer for netCDF4/CF.

Example usage
-------------

The CF writer saves datasets in a Scene as `CF-compliant`_ netCDF file. Here is an example with MSG SEVIRI data in HRIT
format:

    >>> from satpy import Scene
    >>> import glob
    >>> filenames = glob.glob('data/H*201903011200*')
    >>> scn = Scene(filenames=filenames, reader='seviri_l1b_hrit')
    >>> scn.load(['VIS006', 'IR_108'])
    >>> scn.save_datasets(writer='cf', datasets=['VIS006', 'IR_108'], filename='seviri_test.nc',
                          exclude_attrs=['raw_metadata'])

* You can select the netCDF backend using the ``engine`` keyword argument. If `None` if follows
  :meth:`~xarray.Dataset.to_netcdf` engine choices with a preference for 'netcdf4'.
* For datasets with area definition you can exclude lat/lon coordinates by setting ``include_lonlats=False``.
  If the area has a projected CRS, units are assumed to be in metre.  If the
  area has a geographic CRS, units are assumed to be in degrees.  The writer
  does not verify that the CRS is supported by the CF conventions.  One
  commonly used projected CRS not supported by the CF conventions is the
  equirectangular projection, such as EPSG 4087.
* By default non-dimensional coordinates (such as scanline timestamps) are prefixed with the corresponding
  dataset name. This is because they are likely to be different for each dataset. If a non-dimensional
  coordinate is identical for all datasets, the prefix can be removed by setting ``pretty=True``.
* Some dataset names start with a digit, like AVHRR channels 1, 2, 3a, 3b, 4 and 5. This doesn't comply with CF
  https://cfconventions.org/Data/cf-conventions/cf-conventions-1.7/build/ch02s03.html. These channels are prefixed
  with `CHANNEL_` by default. This can be controlled with the variable `numeric_name_prefix` to `save_datasets`.
  Setting it to `None` or `''` will skip the prefixing.

Grouping
~~~~~~~~

All datasets to be saved must have the same projection coordinates ``x`` and ``y``. If a scene holds datasets with
different grids, the CF compliant workaround is to save the datasets to separate files. Alternatively, you can save
datasets with common grids in separate netCDF groups as follows:

    >>> scn.load(['VIS006', 'IR_108', 'HRV'])
    >>> scn.save_datasets(writer='cf', datasets=['VIS006', 'IR_108', 'HRV'],
                          filename='seviri_test.nc', exclude_attrs=['raw_metadata'],
                          groups={'visir': ['VIS006', 'IR_108'], 'hrv': ['HRV']})

Note that the resulting file will not be fully CF compliant.


Dataset Encoding
~~~~~~~~~~~~~~~~

Dataset encoding can be specified in two ways:

1) Via the ``encoding`` keyword argument of ``save_datasets``:

    >>> my_encoding = {
    ...    'my_dataset_1': {
    ...        'zlib': True,
    ...        'complevel': 9,
    ...        'scale_factor': 0.01,
    ...        'add_offset': 100,
    ...        'dtype': np.int16
    ...     },
    ...    'my_dataset_2': {
    ...        'zlib': False
    ...     }
    ... }
    >>> scn.save_datasets(writer='cf', filename='encoding_test.nc', encoding=my_encoding)


2) Via the ``encoding`` attribute of the datasets in a scene. For example

    >>> scn['my_dataset'].encoding = {'zlib': False}
    >>> scn.save_datasets(writer='cf', filename='encoding_test.nc')

See the `xarray encoding documentation`_ for all encoding options.


Attribute Encoding
~~~~~~~~~~~~~~~~~~

In the above examples, raw metadata from the HRIT files have been excluded. If you want all attributes to be included,
just remove the ``exclude_attrs`` keyword argument. By default, dict-type dataset attributes, such as the raw metadata,
are encoded as a string using json. Thus, you can use json to decode them afterwards:

    >>> import xarray as xr
    >>> import json
    >>> # Save scene to nc-file
    >>> scn.save_datasets(writer='cf', datasets=['VIS006', 'IR_108'], filename='seviri_test.nc')
    >>> # Now read data from the nc-file
    >>> ds = xr.open_dataset('seviri_test.nc')
    >>> raw_mda = json.loads(ds['IR_108'].attrs['raw_metadata'])
    >>> print(raw_mda['RadiometricProcessing']['Level15ImageCalibration']['CalSlope'])
    [0.020865   0.0278287  0.0232411  0.00365867 0.00831811 0.03862197
     0.12674432 0.10396091 0.20503568 0.22231115 0.1576069  0.0352385]


Alternatively it is possible to flatten dict-type attributes by setting ``flatten_attrs=True``. This is more human
readable as it will create a separate nc-attribute for each item in every dictionary. Keys are concatenated with
underscore separators. The `CalSlope` attribute can then be accessed as follows:

    >>> scn.save_datasets(writer='cf', datasets=['VIS006', 'IR_108'], filename='seviri_test.nc',
                          flatten_attrs=True)
    >>> ds = xr.open_dataset('seviri_test.nc')
    >>> print(ds['IR_108'].attrs['raw_metadata_RadiometricProcessing_Level15ImageCalibration_CalSlope'])
    [0.020865   0.0278287  0.0232411  0.00365867 0.00831811 0.03862197
     0.12674432 0.10396091 0.20503568 0.22231115 0.1576069  0.0352385]

This is what the corresponding ``ncdump`` output would look like in this case:

.. code-block:: none

    $ ncdump -h test_seviri.nc
    ...
    IR_108:raw_metadata_RadiometricProcessing_Level15ImageCalibration_CalOffset = -1.064, ...;
    IR_108:raw_metadata_RadiometricProcessing_Level15ImageCalibration_CalSlope = 0.021, ...;
    IR_108:raw_metadata_RadiometricProcessing_MPEFCalFeedback_AbsCalCoeff = 0.021, ...;
    ...


.. _CF-compliant: http://cfconventions.org/
.. _xarray encoding documentation:
    http://xarray.pydata.org/en/stable/user-guide/io.html?highlight=encoding#writing-encoded-data
"""

import copy
import json
import logging
import warnings
from collections import OrderedDict, defaultdict
from datetime import datetime
from distutils.version import LooseVersion

import numpy as np
import xarray as xr
from dask.base import tokenize
from pyresample.geometry import AreaDefinition, SwathDefinition
from xarray.coding.times import CFDatetimeCoder

from satpy.writers import Writer
from satpy.writers.utils import flatten_dict

logger = logging.getLogger(__name__)

EPOCH = u"seconds since 1970-01-01 00:00:00"

# Check availability of either netCDF4 or h5netcdf package
try:
    import netCDF4
except ImportError:
    netCDF4 = None

try:
    import h5netcdf
except ImportError:
    h5netcdf = None

# Ensure that either netCDF4 or h5netcdf is available to avoid silent failure
if netCDF4 is None and h5netcdf is None:
    raise ImportError('Ensure that the netCDF4 or h5netcdf package is installed.')

# Numpy datatypes compatible with all netCDF4 backends. ``np.unicode_`` is
# excluded because h5py (and thus h5netcdf) has problems with unicode, see
# https://github.com/h5py/h5py/issues/624."""
NC4_DTYPES = [np.dtype('int8'), np.dtype('uint8'),
              np.dtype('int16'), np.dtype('uint16'),
              np.dtype('int32'), np.dtype('uint32'),
              np.dtype('int64'), np.dtype('uint64'),
              np.dtype('float32'), np.dtype('float64'),
              np.string_]

# Unsigned and int64 isn't CF 1.7 compatible
# Note: Unsigned and int64 are CF 1.9 compatible
CF_DTYPES = [np.dtype('int8'),
             np.dtype('int16'),
             np.dtype('int32'),
             np.dtype('float32'),
             np.dtype('float64'),
             np.string_]

CF_VERSION = 'CF-1.7'


def create_grid_mapping(area):
    """Create the grid mapping instance for `area`."""
    import pyproj
    if LooseVersion(pyproj.__version__) < LooseVersion('2.4.1'):
        # technically 2.2, but important bug fixes in 2.4.1
        raise ImportError("'cf' writer requires pyproj 2.4.1 or greater")
    # let pyproj do the heavily lifting
    # pyproj 2.0+ required
    grid_mapping = area.crs.to_cf()
    return area.area_id, grid_mapping


def get_extra_ds(dataset, keys=None):
    """Get the extra datasets associated to *dataset*."""
    ds_collection = {}
    for ds in dataset.attrs.get('ancillary_variables', []):
        if keys and ds.name not in keys:
            keys.append(ds.name)
            ds_collection.update(get_extra_ds(ds, keys))
    ds_collection[dataset.attrs['name']] = dataset

    return ds_collection


def area2lonlat(dataarray):
    """Convert an area to longitudes and latitudes."""
    dataarray = dataarray.copy()
    area = dataarray.attrs['area']
    ignore_dims = {dim: 0 for dim in dataarray.dims if dim not in ['x', 'y']}
    chunks = getattr(dataarray.isel(**ignore_dims), 'chunks', None)
    lons, lats = area.get_lonlats(chunks=chunks)
    dataarray['longitude'] = xr.DataArray(lons, dims=['y', 'x'],
                                          attrs={'name': "longitude",
                                                 'standard_name': "longitude",
                                                 'units': 'degrees_east'},
                                          name='longitude')
    dataarray['latitude'] = xr.DataArray(lats, dims=['y', 'x'],
                                         attrs={'name': "latitude",
                                                'standard_name': "latitude",
                                                'units': 'degrees_north'},
                                         name='latitude')
    return dataarray


def area2gridmapping(dataarray):
    """Convert an area to at CF grid mapping."""
    dataarray = dataarray.copy()
    area = dataarray.attrs['area']
    gmapping_var_name, attrs = create_grid_mapping(area)
    dataarray.attrs['grid_mapping'] = gmapping_var_name
    return dataarray, xr.DataArray(0, attrs=attrs, name=gmapping_var_name)


def area2cf(dataarray, strict=False, got_lonlats=False):
    """Convert an area to at CF grid mapping or lon and lats."""
    res = []
    if not got_lonlats and (isinstance(dataarray.attrs['area'], SwathDefinition) or strict):
        dataarray = area2lonlat(dataarray)
    if isinstance(dataarray.attrs['area'], AreaDefinition):
        dataarray, gmapping = area2gridmapping(dataarray)
        res.append(gmapping)
    res.append(dataarray)
    return res


def make_time_bounds(start_times, end_times):
    """Create time bounds for the current *dataarray*."""
    start_time = min(start_time for start_time in start_times
                     if start_time is not None)
    end_time = min(end_time for end_time in end_times
                   if end_time is not None)
    data = xr.DataArray([[np.datetime64(start_time), np.datetime64(end_time)]],
                        dims=['time', 'bnds_1d'])
    return data


def assert_xy_unique(datas):
    """Check that all datasets share the same projection coordinates x/y."""
    unique_x = set()
    unique_y = set()
    for dataset in datas.values():
        if 'y' in dataset.dims:
            token_y = tokenize(dataset['y'].data)
            unique_y.add(token_y)
        if 'x' in dataset.dims:
            token_x = tokenize(dataset['x'].data)
            unique_x.add(token_x)
    if len(unique_x) > 1 or len(unique_y) > 1:
        raise ValueError('Datasets to be saved in one file (or one group) must have identical projection coordinates. '
                         'Please group them by area or save them in separate files.')


def link_coords(datas):
    """Link dataarrays and coordinates.

    If the `coordinates` attribute of a data array links to other dataarrays in the scene, for example
    `coordinates='lon lat'`, add them as coordinates to the data array and drop that attribute. In the final call to
    `xr.Dataset.to_netcdf()` all coordinate relations will be resolved and the `coordinates` attributes be set
    automatically.

    """
    for da_name, data in datas.items():
        declared_coordinates = data.attrs.get('coordinates', [])
        if isinstance(declared_coordinates, str):
            declared_coordinates = declared_coordinates.split(' ')
        for coord in declared_coordinates:
            if coord not in data.coords:
                try:
                    dimensions_not_in_data = list(set(datas[coord].dims) - set(data.dims))
                    data[coord] = datas[coord].squeeze(dimensions_not_in_data, drop=True)
                except KeyError:
                    warnings.warn('Coordinate "{}" referenced by dataarray {} does not exist, dropping reference.'
                                  .format(coord, da_name))
                    continue

        # Drop 'coordinates' attribute in any case to avoid conflicts in xr.Dataset.to_netcdf()
        data.attrs.pop('coordinates', None)


def dataset_is_projection_coords(dataset):
    """Check if dataset is a projection coords."""
    if 'standard_name' in dataset.attrs and dataset.attrs['standard_name'] in ['longitude', 'latitude']:
        return True
    return False


def has_projection_coords(ds_collection):
    """Check if collection has a projection coords among data arrays."""
    for dataset in ds_collection.values():
        if dataset_is_projection_coords(dataset):
            return True
    return False


def make_alt_coords_unique(datas, pretty=False):
    """Make non-dimensional coordinates unique among all datasets.

    Non-dimensional (or alternative) coordinates, such as scanline timestamps, may occur in multiple datasets with
    the same name and dimension but different values. In order to avoid conflicts, prepend the dataset name to the
    coordinate name. If a non-dimensional coordinate is unique among all datasets and ``pretty=True``, its name will not
    be modified.

    Since all datasets must have the same projection coordinates, this is not applied to latitude and longitude.

    Args:
        datas (dict):
            Dictionary of (dataset name, dataset)
        pretty (bool):
            Don't modify coordinate names, if possible. Makes the file prettier, but possibly less consistent.

    Returns:
        Dictionary holding the updated datasets

    """
    # Determine which non-dimensional coordinates are unique
    tokens = defaultdict(set)
    for dataset in datas.values():
        for coord_name in dataset.coords:
            if not dataset_is_projection_coords(dataset[coord_name]) and coord_name not in dataset.dims:
                tokens[coord_name].add(tokenize(dataset[coord_name].data))
    coords_unique = dict([(coord_name, len(tokens) == 1) for coord_name, tokens in tokens.items()])

    # Prepend dataset name, if not unique or no pretty-format desired
    new_datas = datas.copy()
    for coord_name, unique in coords_unique.items():
        if not pretty or not unique:
            if pretty:
                warnings.warn('Cannot pretty-format "{}" coordinates because they are not unique among the '
                              'given datasets'.format(coord_name))
            for ds_name, dataset in datas.items():
                if coord_name in dataset.coords:
                    rename = {coord_name: '{}_{}'.format(ds_name, coord_name)}
                    new_datas[ds_name] = new_datas[ds_name].rename(rename)

    return new_datas


class AttributeEncoder(json.JSONEncoder):
    """JSON encoder for dataset attributes."""

    def default(self, obj):
        """Return a json-serializable object for *obj*.

        In order to facilitate decoding, elements in dictionaries, lists/tuples and multi-dimensional arrays are
        encoded recursively.
        """
        if isinstance(obj, dict):
            serialized = {}
            for key, val in obj.items():
                serialized[key] = self.default(val)
            return serialized
        elif isinstance(obj, (list, tuple, np.ndarray)):
            return [self.default(item) for item in obj]
        return self._encode(obj)

    def _encode(self, obj):
        """Encode the given object as a json-serializable datatype."""
        if isinstance(obj, (bool, np.bool_)):
            # Bool has to be checked first, because it is a subclass of int
            return str(obj).lower()
        elif isinstance(obj, (int, float, str)):
            return obj
        elif isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.void):
            return tuple(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()

        return str(obj)


def _encode_nc(obj):
    """Try to encode `obj` as a netcdf compatible datatype which most closely resembles the object's nature.

    Raises:
        ValueError if no such datatype could be found

    """
    if isinstance(obj, int) and not isinstance(obj, (bool, np.bool_)):
        return obj
    elif isinstance(obj, (float, str, np.integer, np.floating)):
        return obj
    elif isinstance(obj, np.ndarray):
        # Only plain 1-d arrays are supported. Skip record arrays and multi-dimensional arrays.
        is_plain_1d = not obj.dtype.fields and len(obj.shape) <= 1
        if is_plain_1d:
            if obj.dtype in NC4_DTYPES:
                return obj
            elif obj.dtype == np.bool_:
                # Boolean arrays are not supported, convert to array of strings.
                return [s.lower() for s in obj.astype(str)]
            return obj.tolist()

    raise ValueError('Unable to encode')


def encode_nc(obj):
    """Encode the given object as a netcdf compatible datatype."""
    try:
        return obj.to_cf()
    except AttributeError:
        return _encode_python_objects(obj)


def _encode_python_objects(obj):
    """Try to find the datatype which most closely resembles the object's nature.

    If on failure, encode as a string. Plain lists are encoded recursively.
    """
    if isinstance(obj, (list, tuple)) and all([not isinstance(item, (list, tuple)) for item in obj]):
        return [encode_nc(item) for item in obj]
    try:
        dump = _encode_nc(obj)
    except ValueError:
        try:
            # Decode byte-strings
            decoded = obj.decode()
        except AttributeError:
            decoded = obj
        dump = json.dumps(decoded, cls=AttributeEncoder).strip('"')
    return dump


def encode_attrs_nc(attrs):
    """Encode dataset attributes in a netcdf compatible datatype.

    Args:
        attrs (dict):
            Attributes to be encoded
    Returns:
        dict: Encoded (and sorted) attributes

    """
    encoded_attrs = []
    for key, val in sorted(attrs.items()):
        if val is not None:
            encoded_attrs.append((key, encode_nc(val)))
    return OrderedDict(encoded_attrs)


def _set_default_chunks(encoding, dataset):
    """Update encoding to preserve current dask chunks.

    Existing user-defined chunks take precedence.
    """
    for var_name, variable in dataset.variables.items():
        if variable.chunks:
            chunks = tuple(
                np.stack([variable.data.chunksize,
                          variable.shape]).min(axis=0)
            )  # Chunksize may not exceed shape
            encoding.setdefault(var_name, {})
            encoding[var_name].setdefault('chunksizes', chunks)


def _set_default_fill_value(encoding, dataset):
    """Set default fill values.

    Avoid _FillValue attribute being added to coordinate variables
    (https://github.com/pydata/xarray/issues/1865).
    """
    coord_vars = []
    for data_array in dataset.values():
        coord_vars.extend(set(data_array.dims).intersection(data_array.coords))
    for coord_var in coord_vars:
        encoding.setdefault(coord_var, {})
        encoding[coord_var].update({'_FillValue': None})


def _set_default_time_encoding(encoding, dataset):
    """Set default time encoding.

    Make sure time coordinates and bounds have the same units. Default is xarray's CF datetime
    encoding, which can be overridden by user-defined encoding.
    """
    if 'time' in dataset:
        try:
            dtnp64 = dataset['time'].data[0]
        except IndexError:
            dtnp64 = dataset['time'].data

        default = CFDatetimeCoder().encode(xr.DataArray(dtnp64))
        time_enc = {'units': default.attrs['units'], 'calendar': default.attrs['calendar']}
        time_enc.update(encoding.get('time', {}))
        bounds_enc = {'units': time_enc['units'],
                      'calendar': time_enc['calendar'],
                      '_FillValue': None}
        encoding['time'] = time_enc
        encoding['time_bnds'] = bounds_enc  # FUTURE: Not required anymore with xarray-0.14+


def _set_encoding_dataset_names(encoding, dataset, numeric_name_prefix):
    """Set Netcdf variable names encoding according to numeric_name_prefix.

    A lot of channel names in satpy starts with a digit. When writing data with the satpy_cf_nc
    these channels are prepended with numeric_name_prefix.
    This ensures this is also done with any matching variables in encoding.
    """
    for _var_name, _variable in dataset.variables.items():
        if not numeric_name_prefix or not _var_name.startswith(numeric_name_prefix):
            continue
        _orig_var_name = _var_name.replace(numeric_name_prefix, '')
        if _orig_var_name in encoding:
            encoding[_var_name] = encoding.pop(_orig_var_name)


def update_encoding(dataset, to_netcdf_kwargs, numeric_name_prefix='CHANNEL_'):
    """Update encoding.

    Preserve dask chunks, avoid fill values in coordinate variables and make sure that
    time & time bounds have the same units.
    """
    other_to_netcdf_kwargs = to_netcdf_kwargs.copy()
    encoding = other_to_netcdf_kwargs.pop('encoding', {}).copy()

    _set_encoding_dataset_names(encoding, dataset, numeric_name_prefix)
    _set_default_chunks(encoding, dataset)
    _set_default_fill_value(encoding, dataset)
    _set_default_time_encoding(encoding, dataset)

    return encoding, other_to_netcdf_kwargs


def _handle_dataarray_name(original_name, numeric_name_prefix):
    name = original_name
    if name[0].isdigit():
        if numeric_name_prefix:
            name = numeric_name_prefix + original_name
        else:
            warnings.warn('Invalid NetCDF dataset name: {} starts with a digit.'.format(name))
    return original_name, name


def _get_compression(compression):
    warnings.warn("The default behaviour of the CF writer will soon change to not compress data by default.",
                  FutureWarning)
    if compression is None:
        compression = {'zlib': True}
    else:
        warnings.warn("The `compression` keyword will soon be deprecated. Please use the `encoding` of the "
                      "DataArrays to tune compression from now on.", FutureWarning)
    return compression


def _set_history(attrs):
    """Add 'history' attribute to the header_attrs."""
    _history_create = 'Created by pytroll/satpy on {}'.format(datetime.utcnow())
    if 'history' in attrs:
        if isinstance(attrs['history'], list):
            attrs['history'] = ''.join(attrs['history'])
        attrs['history'] += '\n' + _history_create
    else:
        attrs['history'] = _history_create


def _get_groups(groups, list_datarrays):
    """Return a dictionary with the list of xr.DataArray associated to each group."""
    # If no groups, return all DataArray attached to a single None key
    if groups is None:
        grouped_dataarrays = {None: list_datarrays}
    # Else, collect the DataArrays associated to each group
    else:
        grouped_dataarrays = defaultdict(list)
        for datarray in list_datarrays:
            for group_name, group_members in groups.items():
                if datarray.attrs['name'] in group_members:
                    grouped_dataarrays[group_name].append(datarray)
                    break
    return grouped_dataarrays


def collect_cf_datasets(list_dataarrays,
                        header_attrs=None,
                        exclude_attrs=None,
                        flatten_attrs=False,
                        pretty=True,
                        include_lonlats=True,
                        epoch=EPOCH,
                        include_orig_name=True,
                        numeric_name_prefix='CHANNEL_',
                        compression=None,  # TODO  [DEPRECATED]
                        groups=None):
    """Process a list of xr.DataArray and return a dictionary with CF-compliant xr.Datasets.

    If the xr.DataArrays does not share the same dimensions, it creates a collection
    of xr.Datasets sharing the same dimensions.

    Parameters
    ----------
    datasets (list):
        List of Satpy Scene datasets to include in the output xr.Dataset.
        The list must include either dataset names or DataIDs.
        If None (the default), it include all loaded Scene datasets.
    header_attrs:
        Global attributes of the output xr.Dataset.
    epoch (str):
        Reference time for encoding the time coordinates (if available).
        Example format: "seconds since 1970-01-01 00:00:00".
        If None, the default reference time is retrieved using `from satpy.cf_writer import EPOCH`
    flatten_attrs (bool):
        If True, flatten dict-type attributes.
    exclude_attrs (list):
        List of xr.DataArray attribute names to be excluded.
    include_lonlats (bool):
        If True, it includes 'latitude' and 'longitude' coordinates also for satpy scene defined on an AreaDefinition.
        If the 'area' attribute is a SwathDefinition, it always include latitude and longitude coordinates.
    pretty (bool):
        Don't modify coordinate names, if possible. Makes the file prettier, but possibly less consistent.
    include_orig_name (bool).
        Include the original dataset name as a variable attribute in the xr.Dataset.
    numeric_name_prefix (str):
        Prefix to add the each variable with name starting with a digit.
        Use '' or None to leave this out.
    groups (dict):
        Group datasets according to the given assignment:
            `{'<group_name>': ['dataset_name1', 'dataset_name2', ...]}`.
        It is used to create grouped netCDFs using the CF_Writer.
        If None (the default), no groups will be created.

    Returns
    -------
    grouped_datasets : dict
        A dictionary of CF-compliant xr.Dataset: {group_name: xr.Dataset}
    header_attrs : dict
        Global attributes to be attached to the xr.Dataset / netCDF4.
    """
    # Check some DataArray have been provided
    if not list_dataarrays:
        raise RuntimeError("None of the requested datasets have been "
                           "generated or could not be loaded. Requested "
                           "composite inputs may need to have matching "
                           "dimensions (eg. through resampling).")

    # Define file header attributes
    if header_attrs is not None:
        if flatten_attrs:
            header_attrs = flatten_dict(header_attrs)
        header_attrs = encode_attrs_nc(header_attrs)  # OrderedDict
    else:
        header_attrs = {}

    # Retrieve groups
    # - If groups is None: {None: list_dataarrays}
    # - if groups not None: {group_name: [xr.DataArray, xr.DataArray ,..], ...}
    # - TODO: if all dataset names are wrong, currently and before the PR behave like groups = None !
    grouped_dataarrays = _get_groups(groups, list_dataarrays)
    is_grouped = len(grouped_dataarrays) >= 2

    # Update header_attrs with 'history' and 'Conventions'
    # - Add "Created by pytroll/satpy ..." to history attribute
    _set_history(header_attrs)
    # - Add CF conventions if not grouped. If 'Conventions' key already present, do not overwrite
    if "Conventions" not in header_attrs and not is_grouped:
        header_attrs['Conventions'] = CF_VERSION

    # TODO REFACTOR
    # Temporary create CF writer instance to acces to CFWriter._collect_datasets
    # Requires refactor of CFWriter to define CFWriter._collect_datasets, and CFWriter.da2cf as generic functions
    # CFWriter.da2cf is a static method and could be put outside
    # CFWriter._collect_datasets(self, ...) could be put outside
    from satpy.writers import load_writer
    cf_writer, save_kwargs = load_writer(writer="cf", filename="")

    # Create dictionary of group xr.Datasets
    # --> If no groups (groups=None) --> group_name=None
    grouped_datasets = {}
    for group_name, group_dataarrays in grouped_dataarrays.items():
        # XXX: Should we combine the info of all datasets?
        dict_datarrays, start_times, end_times = cf_writer._collect_datasets(
            datasets=group_dataarrays,
            epoch=epoch,
            flatten_attrs=flatten_attrs,
            exclude_attrs=exclude_attrs,
            include_lonlats=include_lonlats,
            pretty=pretty,
            compression=compression,
            include_orig_name=include_orig_name,
            numeric_name_prefix=numeric_name_prefix)
        ds = xr.Dataset(dict_datarrays)

        # If no groups, add global header to xr.Dataset
        if not is_grouped:
            ds.attrs = header_attrs

        # Add time_bnds
        if 'time' in ds:
            ds['time_bnds'] = make_time_bounds(start_times, end_times)
            ds['time'].attrs['bounds'] = "time_bnds"
            ds['time'].attrs['standard_name'] = "time"
        else:
            grp_str = ' of group {}'.format(group_name) if group_name is not None else ''
            logger.warning('No time dimension in datasets{}, skipping time bounds creation.'.format(grp_str))

        # Add xr.Dataset to dictionary
        grouped_datasets[group_name] = ds

    # Return dictionary with xr.Dataset
    return grouped_datasets, header_attrs


class CFWriter(Writer):
    """Writer producing NetCDF/CF compatible datasets."""

    @staticmethod
    def da2cf(dataarray, epoch=EPOCH, flatten_attrs=False, exclude_attrs=None, compression=None,
              include_orig_name=True, numeric_name_prefix='CHANNEL_'):
        """Convert the dataarray to something cf-compatible.

        Args:
            dataarray (xr.DataArray):
                The data array to be converted
            epoch (str):
                Reference time for encoding of time coordinates
            flatten_attrs (bool):
                If True, flatten dict-type attributes
            exclude_attrs (list):
                List of dataset attributes to be excluded
            include_orig_name (bool):
                Include the original dataset name in the netcdf variable attributes
            numeric_name_prefix (str):
                Prepend dataset name with this if starting with a digit
        """
        if exclude_attrs is None:
            exclude_attrs = []

        original_name = None
        new_data = dataarray.copy()
        if 'name' in new_data.attrs:
            name = new_data.attrs.pop('name')
            original_name, name = _handle_dataarray_name(name, numeric_name_prefix)
            new_data = new_data.rename(name)

        CFWriter._remove_satpy_attributes(new_data)

        new_data = CFWriter._encode_time(new_data, epoch)
        new_data = CFWriter._encode_coords(new_data)

        # Remove area as well as user-defined attributes
        for key in ['area'] + exclude_attrs:
            new_data.attrs.pop(key, None)

        anc = [ds.attrs['name']
               for ds in new_data.attrs.get('ancillary_variables', [])]
        if anc:
            new_data.attrs['ancillary_variables'] = ' '.join(anc)

        # TODO: make this a grid mapping or lon/lats
        # new_data.attrs['area'] = str(new_data.attrs.get('area'))
        CFWriter._cleanup_attrs(new_data)

        if compression is not None:
            new_data.encoding.update(compression)

        if 'long_name' not in new_data.attrs and 'standard_name' not in new_data.attrs:
            new_data.attrs['long_name'] = new_data.name
        if 'prerequisites' in new_data.attrs:
            new_data.attrs['prerequisites'] = [np.string_(str(prereq)) for prereq in new_data.attrs['prerequisites']]

        if include_orig_name and numeric_name_prefix and original_name and original_name != name:
            new_data.attrs['original_name'] = original_name

        # Flatten dict-type attributes, if desired
        if flatten_attrs:
            new_data.attrs = flatten_dict(new_data.attrs)

        # Encode attributes to netcdf-compatible datatype
        new_data.attrs = encode_attrs_nc(new_data.attrs)

        return new_data

    @staticmethod
    def _cleanup_attrs(new_data):
        for key, val in new_data.attrs.copy().items():
            if val is None:
                new_data.attrs.pop(key)
            if key == 'ancillary_variables' and val == []:
                new_data.attrs.pop(key)

    @staticmethod
    def _encode_coords(new_data):
        """Encode coordinates."""
        if not new_data.coords.keys() & {"x", "y", "crs"}:
            # there are no coordinates
            return new_data
        is_projected = CFWriter._is_projected(new_data)
        if is_projected:
            new_data = CFWriter._encode_xy_coords_projected(new_data)
        else:
            new_data = CFWriter._encode_xy_coords_geographic(new_data)
        if 'crs' in new_data.coords:
            new_data = new_data.drop_vars('crs')
        return new_data

    @staticmethod
    def _is_projected(new_data):
        """Guess whether data are projected or not."""
        crs = CFWriter._try_to_get_crs(new_data)
        if crs:
            return crs.is_projected
        units = CFWriter._try_get_units_from_coords(new_data)
        if units:
            if units.endswith("m"):
                return True
            if units.startswith("degrees"):
                return False
        logger.warning("Failed to tell if data are projected. Assuming yes.")
        return True

    @staticmethod
    def _try_to_get_crs(new_data):
        """Try to get a CRS from attributes."""
        if "area" in new_data.attrs:
            if isinstance(new_data.attrs["area"], AreaDefinition):
                return new_data.attrs["area"].crs
            # at least one test case passes an area of type str
            logger.warning(
                f"Could not tell CRS from area of type {type(new_data.attrs['area']).__name__:s}. "
                "Assuming projected CRS.")
        if "crs" in new_data.coords:
            return new_data.coords["crs"].item()

    @staticmethod
    def _try_get_units_from_coords(new_data):
        for c in "xy":
            if "units" in new_data.coords[c].attrs:
                return new_data.coords[c].attrs["units"]

    @staticmethod
    def _encode_xy_coords_projected(new_data):
        """Encode coordinates, assuming projected CRS."""
        if 'x' in new_data.coords:
            new_data['x'].attrs['standard_name'] = 'projection_x_coordinate'
            new_data['x'].attrs['units'] = 'm'
        if 'y' in new_data.coords:
            new_data['y'].attrs['standard_name'] = 'projection_y_coordinate'
            new_data['y'].attrs['units'] = 'm'
        return new_data

    @staticmethod
    def _encode_xy_coords_geographic(new_data):
        """Encode coordinates, assuming geographic CRS."""
        if 'x' in new_data.coords:
            new_data['x'].attrs['standard_name'] = 'longitude'
            new_data['x'].attrs['units'] = 'degrees_east'
        if 'y' in new_data.coords:
            new_data['y'].attrs['standard_name'] = 'latitude'
            new_data['y'].attrs['units'] = 'degrees_north'
        return new_data

    @staticmethod
    def _encode_time(new_data, epoch):
        if 'time' in new_data.coords:
            new_data['time'].encoding['units'] = epoch
            new_data['time'].attrs['standard_name'] = 'time'
            new_data['time'].attrs.pop('bounds', None)
            new_data = CFWriter._add_time_dimension(new_data)
        return new_data

    @staticmethod
    def _add_time_dimension(new_data):
        if 'time' not in new_data.dims and new_data["time"].size not in new_data.shape:
            new_data = new_data.expand_dims('time')
        return new_data

    @staticmethod
    def _remove_satpy_attributes(new_data):
        # Remove _satpy* attributes
        satpy_attrs = [key for key in new_data.attrs if key.startswith('_satpy')]
        for satpy_attr in satpy_attrs:
            new_data.attrs.pop(satpy_attr)
        new_data.attrs.pop('_last_resampler', None)

    @staticmethod
    def update_encoding(dataset, to_netcdf_kwargs):
        """Update encoding info (deprecated)."""
        warnings.warn('CFWriter.update_encoding is deprecated. '
                      'Use satpy.writers.cf_writer.update_encoding instead.',
                      DeprecationWarning)
        return update_encoding(dataset, to_netcdf_kwargs)

    def save_dataset(self, dataset, filename=None, fill_value=None, **kwargs):
        """Save the *dataset* to a given *filename*."""
        return self.save_datasets([dataset], filename, **kwargs)

    def _collect_datasets(self, datasets, epoch=EPOCH, flatten_attrs=False, exclude_attrs=None, include_lonlats=True,
                          pretty=False, compression=None, include_orig_name=True, numeric_name_prefix='CHANNEL_'):
        """Collect and prepare datasets to be written."""
        ds_collection = {}
        for ds in datasets:
            ds_collection.update(get_extra_ds(ds))
        got_lonlats = has_projection_coords(ds_collection)
        datas = {}
        start_times = []
        end_times = []
        # sort by name, but don't use the name
        for _, ds in sorted(ds_collection.items()):
            if ds.dtype not in CF_DTYPES:
                warnings.warn('Dtype {} not compatible with {}.'.format(str(ds.dtype), CF_VERSION))
            # we may be adding attributes, coordinates, or modifying the
            # structure of attributes
            ds = ds.copy(deep=True)
            try:
                new_datasets = area2cf(ds, strict=include_lonlats, got_lonlats=got_lonlats)
            except KeyError:
                new_datasets = [ds]
            for new_ds in new_datasets:
                start_times.append(new_ds.attrs.get("start_time", None))
                end_times.append(new_ds.attrs.get("end_time", None))
                new_var = self.da2cf(new_ds, epoch=epoch, flatten_attrs=flatten_attrs,
                                     exclude_attrs=exclude_attrs, compression=compression,
                                     include_orig_name=include_orig_name,
                                     numeric_name_prefix=numeric_name_prefix)
                datas[new_var.name] = new_var

        # Check and prepare coordinates
        assert_xy_unique(datas)
        link_coords(datas)
        datas = make_alt_coords_unique(datas, pretty=pretty)

        return datas, start_times, end_times

    def save_datasets(self, datasets, filename=None, groups=None, header_attrs=None, engine=None, epoch=EPOCH,
                      flatten_attrs=False, exclude_attrs=None, include_lonlats=True, pretty=False,
                      compression=None, include_orig_name=True, numeric_name_prefix='CHANNEL_', **to_netcdf_kwargs):
        """Save the given datasets in one netCDF file.

        Note that all datasets (if grouping: in one group) must have the same projection coordinates.

        Args:
            datasets (list):
                List of xr.DataArray to be saved.
            filename (str):
                Output file
            groups (dict):
                Group datasets according to the given assignment: `{'group_name': ['dataset1', 'dataset2', ...]}`.
                Group name `None` corresponds to the root of the file, i.e. no group will be created.
                Warning: The results will not be fully CF compliant!
            header_attrs:
                Global attributes to be included.
            engine (str):
                Module to be used for writing netCDF files. Follows xarray's
                :meth:`~xarray.Dataset.to_netcdf` engine choices with a
                preference for 'netcdf4'.
            epoch (str):
                Reference time for encoding of time coordinates.
            flatten_attrs (bool):
                If True, flatten dict-type attributes.
            exclude_attrs (list):
                List of dataset attributes to be excluded.
            include_lonlats (bool):
                Always include latitude and longitude coordinates, even for datasets with area definition.
            pretty (bool):
                Don't modify coordinate names, if possible. Makes the file prettier, but possibly less consistent.
            compression (dict):
                Compression to use on the datasets before saving, for example {'zlib': True, 'complevel': 9}.
                This is in turn passed the xarray's `to_netcdf` method:
                http://xarray.pydata.org/en/stable/generated/xarray.Dataset.to_netcdf.html for more possibilities.
                (This parameter is now being deprecated, please use the DataArrays's `encoding` from now on.)
            include_orig_name (bool).
                Include the original dataset name as a variable attribute in the final netCDF.
            numeric_name_prefix (str):
                Prefix to add the each variable with name starting with a digit. Use '' or None to leave this out.

        """
        # Note: datasets is a list of xr.DataArray
        logger.info('Saving datasets to NetCDF4/CF.')

        # Retrieve compression [Deprecated]
        compression = _get_compression(compression)

        # Define netCDF filename if not provided
        # - It infers the name from the first DataArray
        filename = filename or self.get_filename(**datasets[0].attrs)

        # Collect xr.Dataset for each group
        grouped_datasets, header_attrs = collect_cf_datasets(list_dataarrays=datasets,  # list of xr.DataArray
                                                             header_attrs=header_attrs,
                                                             exclude_attrs=exclude_attrs,
                                                             flatten_attrs=flatten_attrs,
                                                             pretty=pretty,
                                                             include_lonlats=include_lonlats,
                                                             epoch=epoch,
                                                             include_orig_name=include_orig_name,
                                                             numeric_name_prefix=numeric_name_prefix,
                                                             groups=groups,
                                                             compression=compression,  # [DEPRECATED]
                                                             )
        # Remove satpy-specific kwargs
        # - This kwargs can contain encoding dictionary
        to_netcdf_kwargs = copy.deepcopy(to_netcdf_kwargs)
        satpy_kwargs = ['overlay', 'decorate', 'config_files']
        for kwarg in satpy_kwargs:
            to_netcdf_kwargs.pop(kwarg, None)

        # If writing grouped netCDF, create an empty "root" netCDF file
        # - Add the global attributes
        # - All groups will be appended in the for loop below
        if groups is not None:
            root = xr.Dataset({}, attrs=header_attrs)
            # - Define init kwargs
            init_nc_kwargs = to_netcdf_kwargs.copy()
            init_nc_kwargs.pop('encoding', None)  # No variables to be encoded at this point
            init_nc_kwargs.pop('unlimited_dims', None)
            written = [root.to_netcdf(filename, engine=engine, mode='w', **init_nc_kwargs)]
            mode = "a"
        else:
            mode = "w"
            written = []

        # Write the netCDF
        # - If grouped netCDF, it appends to the root file
        # - If single netCDF, it write directly
        for group_name, ds in grouped_datasets.items():
            # Update encoding
            encoding, other_to_netcdf_kwargs = update_encoding(ds,
                                                               to_netcdf_kwargs,
                                                               numeric_name_prefix)
            # Write (append) dataset
            res = ds.to_netcdf(filename, engine=engine,
                               group=group_name, mode=mode,
                               encoding=encoding,
                               **other_to_netcdf_kwargs)
            written.append(res)

        # Return list of writing results
        return written
