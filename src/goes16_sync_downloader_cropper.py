import s3fs
import os
import time
from datetime import datetime, timedelta
import threading
from concurrent.futures import ThreadPoolExecutor
from osgeo import osr, gdal
import argparse
import logging

########################################################################
### DOWNLOADER
########################################################################

# Lock to synchronize access to shared resources
download_lock = threading.Lock()

# Full disk download function
def download_and_crop_file(fs, file_name, local_path, crop_dir, variable_names):
    """Download a file and put it into the download directory."""
    try:
        fs.get(file_name, local_path)
        # print(f"Processing {file_path}")
        lat_max, lon_max = (
            -21.699774257353113,
            -42.35676996062447,
        )  # canto superior direito
        lat_min, lon_min = (
            -23.801876626302175,
            -45.05290312102409,
        )  # canto inferior esquerdo
        extent = [lon_min, lat_min, lon_max, lat_max]
        crop_full_disk_and_save(full_disk_filename = local_path, 
                                variable_names = variable_names, 
                                extent = extent, 
                                dest_path = crop_dir)
        # print(f"Downloaded and cropped {os.path.basename(file_name)}")
        os.remove(local_path)  # Delete the file after processing
    except Exception as e:
        print(f"Error downloading {os.path.basename(file_name)}: {e}")

# Function to convert a regular date to the Julian day of the year
def get_julian_day(date):
    return date.strftime('%j')

# Function to download files from GOES-16 for a specific hour
def process_goes16_data_for_hour(fs, s3_path, channel, download_dir, crop_dir, variable_names):
    # List all files in the given hour directory
    try:
        files = fs.ls(s3_path)
    except Exception as e:
        print(f"Error accessing S3 path {s3_path}: {e}")
        return

    # Filter files for the specific channel (e.g., C01 for channel 1)
    channel_files = [file for file in files if f'C{channel:02d}' in file]

    # print(f'# files in {s3_path}: {len(channel_files)}')

    # Download each file using threads
    with ThreadPoolExecutor() as executor:
        for file in channel_files:
            file_name = os.path.basename(file)
            local_path = os.path.join(download_dir, file_name)
            if not os.path.exists(local_path):
                executor.submit(download_and_crop_file, fs, file, local_path, crop_dir, variable_names)

# Function to download all files for a given day
def process_goes16_data_for_day(date, channel, download_dir, crop_dir, variable_names):
    # Set up S3 access
    fs = s3fs.S3FileSystem(anon=True)
    bucket = 'noaa-goes16'
    product = f'ABI-L2-CMIPF'

    # Format the date
    year = date.strftime('%Y')
    julian_day = get_julian_day(date)

    # Download data for each hour concurrently
    with download_lock:
        with ThreadPoolExecutor() as executor:
            for hour in range(24):
                hour_str = f'{hour:02d}'
                s3_path = f'{bucket}/{product}/{year}/{julian_day}/{hour_str}/'
                # Submit each hour's download process to the thread pool
                executor.submit(process_goes16_data_for_hour, fs, s3_path, channel, download_dir, crop_dir, variable_names)

# Main function to process files for a range of dates
def process_goes16_data_for_period(start_date, end_date, ignored_months, channel, download_dir, crop_dir, variable_names):
    current_date = start_date
    while current_date <= end_date:
        if current_date.month in ignored_months:
            continue
        print(f"Processing data for {current_date.strftime('%Y-%m-%d')}")
        process_goes16_data_for_day(current_date, channel, download_dir, crop_dir, variable_names)
        current_date += timedelta(days=1)

########################################################################
### CROPPER
########################################################################

def extract_middle_part(file_path):
    # Split the file path by '/' and get the last part (the filename)
    filename = file_path.split('/')[-1]
    
    # The middle part ends right before the '_s' section
    middle_part = filename.split('_s')[0]
    
    return middle_part

def crop_full_disk_and_save(full_disk_filename, variable_names, extent, dest_path):
    for var in variable_names:
   
        # Open the file
        img = gdal.Open(f'NETCDF:{full_disk_filename}:' + var)

        assert (img is not None)

        # Read the header metadata
        metadata = img.GetMetadata()
        scale = float(metadata.get(var + '#scale_factor'))
        offset = float(metadata.get(var + '#add_offset'))
        undef = float(metadata.get(var + '#_FillValue'))

        dtime = metadata.get('NC_GLOBAL#time_coverage_start')
        dtime = datetime.strptime(dtime, '%Y-%m-%dT%H:%M:%S.%fZ')
        yyyymmddhhmn = dtime.strftime('%Y_%m_%d_%H_%M')

        # Load the data
        ds = img.ReadAsArray(0, 0, img.RasterXSize, img.RasterYSize).astype(float)

        # Apply the scale and offset
        ds = (ds * scale + offset)

        # Read the original file projection and configure the output projection
        source_prj = osr.SpatialReference()
        source_prj.ImportFromProj4(img.GetProjectionRef())

        target_prj = osr.SpatialReference()
        target_prj.ImportFromProj4("+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs")

        # Reproject the data
        GeoT = img.GetGeoTransform()
        driver = gdal.GetDriverByName('MEM')
        raw = driver.Create('raw', ds.shape[0], ds.shape[1], 1, gdal.GDT_Float32)
        raw.SetGeoTransform(GeoT)
        raw.GetRasterBand(1).WriteArray(ds)

        # Define the parameters of the output file  
        options = gdal.WarpOptions(format = 'netCDF', 
                srcSRS = source_prj, 
                dstSRS = target_prj,
                outputBounds = (extent[0], extent[3], extent[2], extent[1]), 
                outputBoundsSRS = target_prj, 
                outputType = gdal.GDT_Float32, 
                srcNodata = undef, 
                dstNodata = 'nan', 
                resampleAlg = gdal.GRA_NearestNeighbour)
        
        img = None  # Close file

        # Write the reprojected file on disk
        prefix = extract_middle_part(full_disk_filename)
        # print("prefix: ", prefix)
        filename_reprojected = f'{dest_path}/{prefix}_{var}_{yyyymmddhhmn}.nc'
        # print(f"Saving crop: {filename_reprojected}")
        gdal.Warp(filename_reprojected, raw, options=options)

########################################################################
### MAIN
########################################################################

if __name__ == "__main__":
    '''
    Example usage:
    one-day test
    python src/goes16_sync_downloader_cropper.py --start_date "2024-02-08" --end_date "2024-02-08" --channel 7 --download_dir "./downloads" --crop_dir "./cropped" --vars "CMI"
    '''

    # Create an argument parser to accept start and end dates, and channel number from the command line
    parser = argparse.ArgumentParser(description="Download GOES-16 data for a specific date range.")
    parser.add_argument('--start_date', type=str, required=True, help="Start date (format: YYYY-MM-DD)")
    parser.add_argument('--end_date', type=str, required=True, help="End date (format: YYYY-MM-DD)")
    parser.add_argument('--channel', type=int, required=True, help="GOES-16 channel number (1-16)")
    parser.add_argument('--download_dir', type=str, default='./downloads', help="Directory to save downloaded FD files")
    parser.add_argument('--crop_dir', type=str, default='./data/goes16/CMI/cropped', help="Directory to save cropped files")
    parser.add_argument("--ignored_months", nargs='+', type=int, required=False, default=[6, 7, 8],
                        help="Months to ignore (e.g., --ignored_months 6 7 8)")
    parser.add_argument("--vars", nargs='+', type=str, required=True, help="At least one variable name (CMI, ...)")

    fmt = "[%(levelname)s] %(funcName)s():%(lineno)i: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)

    args = parser.parse_args()

    # Parse the start and end dates
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
    channel = args.channel
    download_dir = args.download_dir
    crop_dir = args.crop_dir
    ignored_months = args.ignored_months
    variable_names = args.vars

    start_time = time.time()  # Record the start time
    download_dir = './downloads'
    if not os.path.exists(download_dir):
        os.makedirs(download_dir)
    process_goes16_data_for_period(start_date, end_date, ignored_months, channel = channel, download_dir = download_dir, crop_dir = crop_dir, variable_names = variable_names)
    end_time = time.time()  # Record the end time
    duration = (end_time - start_time) / 60  # Calculate duration in minutes
    print(f"Script execution time: {duration:.2f} minutes.")