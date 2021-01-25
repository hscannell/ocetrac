# AUTOGENERATED! DO NOT EDIT! File to edit: 00_core.ipynb (unless otherwise specified).

__all__ = ['track']

# Cell
from nbdev.showdoc import *
import xarray as xr
import numpy as np
import scipy.ndimage
from skimage.measure import label, regionprops
# import matplotlib.pyplot as plt


# Cell
def _morphological_operations(da, radius=8):
    '''Converts xarray.DataArray to binary, defines structuring element, and performs morphological closing then opening.
    Parameters
    ----------
    da     : xarray.DataArray
            The data to label
    radius : int
            Length of grid spacing to define the radius of the structing element used in morphological closing and opening.

    '''

    # Convert images to binary. All positive values == 1, otherwise == 0
    bitmap_binary = da.where(da>0, drop=False, other=0)
    bitmap_binary = bitmap_binary.where(bitmap_binary==0, drop=False, other=1)

    # Define structuring element
    diameter = radius*2
    x = np.arange(-radius, radius+1)
    x, y = np.meshgrid(x, x)
    r = x**2+y**2
    se = r<radius**2

    def binary_open_close(bitmap_binary):
        bitmap_binary_padded = np.pad(bitmap_binary,
                                      ((diameter, diameter), (diameter, diameter)),
                                      mode='reflect')
        s1 = scipy.ndimage.binary_closing(bitmap_binary_padded, se, iterations=1)
        s2 = scipy.ndimage.binary_opening(s1, se, iterations=1)
        unpadded= s2[diameter:-diameter, diameter:-diameter]
        return unpadded

    mo_binary = xr.apply_ufunc(binary_open_close, bitmap_binary,
                               input_core_dims=[['lat', 'lon']],
                               output_core_dims=[['lat', 'lon']],
                               output_dtypes=[bitmap_binary.dtype],
                               vectorize=True,
                               dask='parallelized')

    return mo_binary


# Cell
def _id(binary_images):
    '''label features from binary images'''

    labels, num = xr.apply_ufunc(
        label,
        binary_images,
        kwargs={'return_num': True, 'connectivity': 2},
        input_core_dims=[['lat', 'lon', ]],
        output_core_dims=[['lat', 'lon'], []],
        output_dtypes=['i4', 'i4'],
        dask='parallelized',
        vectorize=True
    )

    #non_core_dims = set(binary_images.dims) - {'lat', 'lon'}
    # TODO: stop assuming 3D images
    offset = num.cumsum().shift(time=1, fill_value=0)
    unique_labels = xr.where(labels > 0, labels + offset, 0)
    return unique_labels

# Cell
def _wrap_labels(labels):
    '''wrap labels that cross prime meridian '''

    prime = labels.loc[dict(lon=labels.lon[:2])]

    prime_ids = np.unique(prime)[(~np.isnan(np.unique(prime)))&(np.unique(prime)>0)].astype('float')
    mirrormapBool = xr.DataArray(np.in1d(labels, prime_ids).reshape(labels.shape),
                                 dims=labels.dims,
                                 coords=labels.coords)
    earth2 = labels.where(mirrormapBool==True, drop=False, other=0)
    earth1 = labels.where(mirrormapBool==False, drop=False, other=0) # Remove label from origonal map

    # Concatenate and convert to binary
    res = labels.lon[1].values-labels.lon[0].values # resolution of longitude
    two_earths = xr.concat([earth1, earth2], dim='lon')
    two_earths['lon'] = np.arange(float(two_earths.lon[0].values),(two_earths.lon[-1].values*2)+res,res)
    bitmap_binary_2E = two_earths.where(two_earths>0, drop=False, other=0)
    bitmap_binary_2E = bitmap_binary_2E.where(bitmap_binary_2E==0, drop=False, other=1)
    bitmap_bool_2E = bitmap_binary_2E>0

    return bitmap_binary_2E, bitmap_bool_2E

# Cell
def _id_area(labels, min_size_quartile):
    '''calculatre area with regionprops'''

    props = regionprops(labels.values.astype('int'))

    labelprops = [p.label for p in props]
    labelprops = xr.DataArray(labelprops, dims=['label'], coords={'label': labelprops})
    coords = [p.coords for p in props] # time, lat, lon

    area = []
    res = labels.lat[1].values-labels.lat[0].values # resolution of latitude
    for i in range(len(coords)):
        area.append(np.sum((res*111)*np.cos(np.radians(labels.lat[coords[i][:,0]].values)) * (res*111)))
    area = xr.DataArray(area, dims=['label'], coords={'label': labelprops})
    min_area = np.percentile(area, min_size_quartile*100)
    print('min area (km2) \t', min_area)

    return area, min_area, labelprops

# Cell
def track(da, radius=8, area_quantile=0.75):
    '''Image labeling and tracking.

    Parameters
    ----------
    da : xarray.DataArray
        The data to label.

    radius : int
        size of the structuring element used in morphological opening and closing.

    area_quantile : float
        quantile used to define the threshold of the smallest area object retained in tracking.

    Returns
    -------
    labels : xarray.DataArray
        Integer labels of the connected regions.
    '''

    # Converts data to binary, defines structuring element, and performs morphological closing then opening
    binary_images = _morphological_operations(da, radius=radius)

    # label features from binary images
    ID = _id(binary_images)

    # wrap labels that cross prime meridian
    bitmap_binary_2E, bitmap_bool_2E = _wrap_labels(ID)

    ### ! Reapply land maks HERE

    # relabel 2D features from binary images that are wrapped around meridian
    ID_wrap = _id(bitmap_binary_2E)

    # calculatre area with regionprops
    area, min_area, labelprops = _id_area(ID_wrap, area_quantile)

    keep_labels = labelprops.where(area>=min_area, drop=True)

    ID_area_bool = xr.DataArray(np.isin(ID_wrap, keep_labels).reshape(ID_wrap.shape),
                               dims=ID_wrap.dims, coords=ID_wrap.coords)

    # Calculate Percent of total MHW area retained
    tot_area = int(np.sum(area.values))
    small_area = area.where(area<=min_area, drop=True)
    small_area = int(np.sum(small_area.values))
    percent_area_kept = 1-(small_area/tot_area)

    features = _id(ID_area_bool)
    features = features.rename('labels')
    features.attrs['min_area'] = min_area
    features.attrs['percent_area_kept'] = percent_area_kept
    print('inital features identified \t', int(features.max().values))

    ## Track labeled features
    bitmap_binary = features.where(features>0, drop=False, other=0)
    bitmap_binary = bitmap_binary.where(bitmap_binary==0, drop=False, other=1)

    ####### Label with Skimage
    # relabel
    label_sk3, final_features = label(bitmap_binary, connectivity=3, return_num=True)
    # explore scikit-image dask image

    label_sk3 = xr.DataArray(label_sk3, dims=['time','lat','lon'],
                          coords={'time': bitmap_binary.time, 'lat': bitmap_binary.lat,'lon': bitmap_binary.lon})
    binary_labels = label_sk3.where(label_sk3>0, drop=False, other=0)
    split_lon = int(binary_labels.shape[2]/2)
    origonal_map = binary_labels[:,:,split_lon:].values + binary_labels[:,:,:split_lon].values
    # Convert labels to DataArray

    labels = xr.DataArray(origonal_map, dims=['time','lat','lon'],
                          coords={'time': bitmap_binary_2E.time, 'lat': bitmap_binary_2E.lat,'lon': bitmap_binary_2E.lon})
    labels = labels.where(labels > 0, drop=False, other=np.nan)


    print('final features tracked \t', final_features)

    return labels