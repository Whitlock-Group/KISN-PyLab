# -*- coding: utf-8 -*-

"""

@author: bartulem

Read in sync events from all desired data streams.

Neuropixel recordings are saved into a *1D binary vector*, from a 2D array organised as 385 rows (channels) x n columns (samples). 
The data are written to file from this matrix in column-major (F) order, i.e., the first sample in the recording was written to file 
for every channel, then the second sample was written for every channel, etc. Neuropixel raw data is provided as an int16 binary.
Neuropixel ADCs are 10 bits, with a range of -0.6 to 0.6 V, and acquisition was at 500x gain, yielding a resolution of 2.34 µV/bit.
To obtain readings in µV, you should multiply the int16 values by 2.34.

This script has the purpose of extracting sync events from three independent data streams: (1) NPX recording files (may be one or
two in a given session, depending on the number of probes), (2) the exported Motive tracking file, and (3) the IMU sensor file,
all of which keep track of the LEDon & LEDoff occurrences. The code goes through all of those files and picks up the LED events,
(taking into account that the LED signal jitters, which can be probe independent) saving the complete data frame to a separate
binary .pkl file. This code consumes a lot of RAM (64Gb should be enough in most cases).

"""

import os
import sys
import numpy as np
from tqdm import tqdm
import gc
import time
import csv
import pandas as pd
import pickle
from astropy.convolution import convolve
from astropy.convolution import Gaussian1DKernel
from collections import Counter
import operator


class EventReader:

    # initializer / instance attributes
    def __init__(self, npx_files, sync_df):
        self.npx_files = npx_files
        self.sync_df = sync_df

    def read_se(self, **kwargs):

        """
        Parameters
        ----------
        **kwargs: dictionary
        nchan : int/float
            Total number of channels on the NPX probe, for Probe3b should be 385; defaults to 385.
        sync_chan : int/float
            Sync port channel number, for Probe3b should be 385; defaults to 385.
        ledoff : boolean (0/False or 1/True)
            Whether to consider the LEDoff times; defaults to 0.
        track_file : str/boolean
            The absolute path to the tracking data .csv file; defaults to 0.
        imu_file : str/boolean
            The absolute path to the IMU data .txt file; defaults to 0.
        imu_pkl : str/boolean
            The absolute path to the IMU data .pkl file; defaults to 0.
        jitter_samples : int/float
            Number of samples in the imec data across which LED jitter could arise; defaults to 3.
        half_smooth_window : int/float
            Number of frames in the tracking data that are smoothed over to correct nans in fully empty frames; defaults to 10.
        ground_probe : int
            In a multi probe setting, the probe other probes are synced to; defaults to 0.
        ----------
        """

        # valid values for booleans
        valid_bools = [0, False, 1, True]

        nchan = int([kwargs['nchan'] if 'nchan' in kwargs.keys() and (type(kwargs['nchan']) == int or type(kwargs['nchan']) == float) else 385][0])
        sync_chan = int([kwargs['sync_chan'] if 'sync_chan' in kwargs.keys() and (type(kwargs['sync_chan']) == int or type(kwargs['sync_chan']) == float) else 385][0])
        ledoff = [kwargs['ledoff'] if 'ledoff' in kwargs.keys() and kwargs['ledoff'] in valid_bools else 0][0]
        track_file = [kwargs['track_file'] if 'track_file' in kwargs.keys() and type(kwargs['track_file']) == str else 0][0]
        imu_file = [kwargs['imu_file'] if 'imu_file' in kwargs.keys() and type(kwargs['imu_file']) == str else 0][0]
        imu_pkl = [kwargs['imu_pkl'] if 'imu_pkl' in kwargs.keys() and type(kwargs['imu_pkl']) == str else 0][0]
        jitter_samples = int([kwargs['jitter_samples'] if 'jitter_samples' in kwargs.keys() and (type(kwargs['jitter_samples']) == int or type(kwargs['jitter_samples']) == float) else 3][0])
        half_smooth_window = int([kwargs['half_smooth_window'] if 'half_smooth_window' in kwargs.keys() and (type(kwargs['half_smooth_window']) == int or type(kwargs['half_smooth_window']) == float) else 10][0])
        ground_probe = int([kwargs['ground_probe'] if 'ground_probe' in kwargs.keys() else 0][0])

        # check that the NPX files are there
        for anpxfile in self.npx_files:
            if not os.path.exists(anpxfile):
                print('Could not find file {}, try again.'.format(anpxfile))
                sys.exit()

        t = time.time()
        print('Extracting sync data from NPX file(s), please be patient - this could take >5 minutes.')

        # initialize dictionary to store the data in
        sync_dict = {}

        # extract LED events from the NPX file(s)
        for anpxfile in self.npx_files:

            # give it a 2s break
            time.sleep(2)

            # memmaps are used for accessing small segments of large files on disk, without reading the entire file into memory.
            npx_recording = np.memmap(anpxfile, mode='r', dtype=np.int16, order='C')

            # integer divide the length of the recording by channel count to get number of samples
            npx_samples = len(npx_recording) // nchan

            # reshape the array such that channels are rows and samples are columns
            npx_recording = npx_recording.reshape((nchan, npx_samples), order='F')

            # get the sync data in a separate array, this is a burden on the system memory
            sync_data = npx_recording[sync_chan - 1, :]

            # find sync events and collect them in a dict / this warrants a more detailed explanation
            # it turns out that even though the LED is on - sometimes there's a couple of samples where the signal goes down
            # such that what is essentially one event - gets recognized as two LED events. To make matters worse, this seems
            # to be at least somewhat probe independent (so some of these events happen on imec0 but not imec1). The way we
            # deal with this here is screening for these 'jitters' and ignoring them as prospective events. However, defining
            # the length of a jitter is arbitrary - here we use 3 samples because it worked best in most test scenarios
            # (but you can set a number other than 3).

            counter = sorted(dict(Counter(sync_data)).items(), key=operator.itemgetter(1), reverse=True)
            most_freq_two_values = [counter[0][0], counter[1][0]]

            session_proportion = (counter[0][1] + counter[1][1]) / len(sync_data)
            if session_proportion < .99:
                print('The two most dominant values, {} and {} appear together only {:.3f} of the total session, so something is wrong. Check it out!'.format(counter[0][0], counter[1][0], session_proportion))
                sys.exit()

            high_val = np.nanmax(most_freq_two_values)
            low_val = np.nanmin(most_freq_two_values)

            change_points = []
            for inxSync, itemSync in tqdm(enumerate(sync_data)):
                if itemSync == high_val and sync_data[inxSync - 1] == low_val and np.sum(sync_data[(inxSync - jitter_samples):inxSync]) == low_val*jitter_samples and np.sum(sync_data[(inxSync + 1):(inxSync + jitter_samples + 1)]) == high_val*jitter_samples:
                    change_points.append(inxSync)
                elif itemSync == low_val and sync_data[inxSync - 1] == high_val and np.sum(sync_data[(inxSync - jitter_samples):inxSync]) == high_val*jitter_samples and np.sum(sync_data[(inxSync + 1):(inxSync + jitter_samples + 1)]) == low_val*jitter_samples:
                    change_points.append(inxSync)

            probe_sync = {}
            counter_on = 1
            counter_off = 1
            probe_sync['Session start'] = 0
            for indx, timestamp in enumerate(change_points):
                if indx == 0:
                    probe_sync['TTL input start'] = timestamp
                elif indx != 0 and indx != (len(change_points) - 1) and indx % 2 != 0:
                    probe_sync['{}LEDon'.format(counter_on)] = timestamp
                    counter_on += 1
                elif indx != 0 and indx != (len(change_points) - 1) and indx % 2 == 0:
                    if ledoff:
                        probe_sync['{}LEDoff'.format(counter_off)] = timestamp
                        counter_off += 1
                    else:
                        continue
                else:
                    probe_sync['TTL input stop'] = timestamp
            probe_sync['Session stop'] = len(sync_data)

            # delete the map object from memory
            del npx_recording
            gc.collect()

            # save data to sync_dict where imec number is key
            sync_dict['imec{}'.format(anpxfile[anpxfile.find('imec') + len('imec')])] = probe_sync
            print('There are {} total LED events in the imec{} file.'.format(len(probe_sync.keys()) - 4, anpxfile[anpxfile.find('imec') + len('imec')]))

        # extract LED events from the tracking file
        if os.path.exists(track_file):

            print('Extracting sync data from the tracking file, please remain patient.')

            # read in the 4th line of the tracking .csv and find the column where LED appears for the first time
            with open(track_file, 'r') as thecsv:
                for rowindx, row in enumerate(csv.reader(thecsv, delimiter=',')):
                    if rowindx == 3:
                        columnofint = next(indx for indx, obj in enumerate(row) if 'LED' in obj)

            # correct fully empty frames / this warrants a more detailed explanation
            # tracking files, for some reason, sometimes get frames where all markers drop out. This happens
            # infrequently and when it does it is only a couple of frames that are surrounded by frames that have
            # all markers present. This can be a problem for LED detection because one empty frame would separate
            # something that was a unitary LED event into two different LED events. To prevent this the next snippet
            # of code goes through the tracking file and interpolates those empty frames by smoothing over a window
            # of 10 rows above and below the frame. You can change that number to something else, but it should not
            # be too large because a large smoothing window can capture true distant LED events and we don't want that.

            corrected_frames = {}
            led_cols = list(range(columnofint, columnofint+9))
            original_tracking_data = pd.read_csv(track_file, sep=',', skiprows=6)

            print('Correcting fully empty frames and jitter-empty LED frames.')

            # give it a 2s break
            time.sleep(2)

            for i in tqdm(range(original_tracking_data.shape[0])):
                if i < (original_tracking_data.shape[0] - 1) and original_tracking_data.iloc[i, 2:].isnull().values.all():
                    changed_array = np.zeros(original_tracking_data.shape[1])
                    for inx in range(len(changed_array)):
                        if inx < 2:
                            if inx < 1:
                                changed_array[inx] = original_tracking_data.iloc[i, inx]
                            else:
                                changed_array[inx] = round(original_tracking_data.iloc[i, inx], 6)
                        elif 1 < inx and inx not in led_cols:
                            smoothed_val = round(convolve(original_tracking_data.iloc[i-half_smooth_window:i+half_smooth_window+1, inx], kernel=Gaussian1DKernel(stddev=1), nan_treatment='interpolate', preserve_nan=False)[half_smooth_window], 6)
                            changed_array[inx] = smoothed_val
                        else:
                            if (np.isnan(original_tracking_data.iloc[i-half_smooth_window:i, inx]).all() and not np.isnan(original_tracking_data.iloc[i+1:i+half_smooth_window+1, inx]).all()) or (not np.isnan(original_tracking_data.iloc[i-half_smooth_window:i, inx]).all() and np.isnan(original_tracking_data.iloc[i+1:i+half_smooth_window+1, inx]).all()):
                                changed_array[inx] = np.nan
                            else:
                                smoothed_val = round(convolve(original_tracking_data.iloc[i-half_smooth_window:i+half_smooth_window+1, inx], kernel=Gaussian1DKernel(stddev=1), nan_treatment='interpolate', preserve_nan=False)[half_smooth_window], 6)
                                changed_array[inx] = smoothed_val

                    # format strings accordingly
                    changed_list = []
                    for indx, item in enumerate(changed_array):
                        if indx == 0:
                            changed_list.append(str(int(item)))
                        else:
                            if not np.isnan(item):
                                changed_list.append('{:.6f}'.format(item))
                            else:
                                changed_list.append('')
                    corrected_frames[i + 7] = changed_list

                elif i < (original_tracking_data.shape[0] - 1) and not original_tracking_data.iloc[i, 2:].isnull().values.all() and original_tracking_data.iloc[i, columnofint:columnofint+9].isnull().values.all() and not original_tracking_data.iloc[i-half_smooth_window:i, columnofint:columnofint+9].isnull().values.all() and not original_tracking_data.iloc[i:i+half_smooth_window+1, columnofint:columnofint+9].isnull().values.all():
                    changed_array = np.zeros(original_tracking_data.shape[1])
                    for inx in range(len(changed_array)):
                        if inx not in led_cols:
                            if inx < 1:
                                changed_array[inx] = original_tracking_data.iloc[i, inx]
                            else:
                                changed_array[inx] = round(original_tracking_data.iloc[i, inx], 6)
                        else:
                            smoothed_val = round(convolve(original_tracking_data.iloc[i-half_smooth_window:i+half_smooth_window+1, inx], kernel=Gaussian1DKernel(stddev=1), nan_treatment='interpolate', preserve_nan=False)[half_smooth_window], 6)
                            changed_array[inx] = smoothed_val

                    # format strings accordingly
                    changed_list = []
                    for indx, item in enumerate(changed_array):
                        if indx == 0:
                            changed_list.append(str(int(item)))
                        else:
                            if not np.isnan(item):
                                changed_list.append('{:.6f}'.format(item))
                            else:
                                changed_list.append('')
                    corrected_frames[i + 7] = changed_list

            # save the interpolated tracking .csv
            with open(track_file, 'r') as rtf, open('{}_interpolated.csv'.format(track_file[:-4]), 'w', newline='') as wtf:
                writer = csv.writer(wtf)
                reader = csv.reader(rtf)
                for rowindx, row in enumerate(reader):
                    if rowindx not in corrected_frames.keys():
                        writer.writerow(row)
                    else:
                        writer.writerow(corrected_frames[rowindx])

            # load the df and search for LEDons/offs
            tracking_data = pd.read_csv('{}_interpolated.csv'.format(track_file[:-4]), sep=',', skiprows=6)

            tracking_sync = {}
            tracking_on = 1
            tracking_off = 1
            all_led_frames = []

            print('Looking for LED events in the interpolated tracking file.')

            # give it a 2s break
            time.sleep(2)

            tracking_sync['TTL input start'] = 0
            for row in tqdm(range(tracking_data.shape[0])):
                if not tracking_data.iloc[row, columnofint:(columnofint + 9)].isnull().values.all() and tracking_data.iloc[row - 1, columnofint:(columnofint + 9)].isnull().values.all() and not tracking_data.iloc[row + int(round(half_smooth_window / 2)) + 1, columnofint:(columnofint + 9)].isnull().values.all():
                    tracking_sync['{}LEDon'.format(tracking_on)] = tracking_data.loc[row, 'Frame']
                    all_led_frames.append(tracking_data.loc[row, 'Frame'])
                    tracking_on += 1
                elif tracking_data.iloc[row, columnofint:(columnofint + 9)].isnull().values.all() and not tracking_data.iloc[row - 1, columnofint:(columnofint + 9)].isnull().values.all() and not tracking_data.iloc[row - int(round(half_smooth_window / 2)), columnofint:(columnofint + 9)].isnull().values.all():
                    if ledoff:
                        tracking_sync['{}LEDoff'.format(tracking_off)] = tracking_data.loc[row, 'Frame']
                        all_led_frames.append(tracking_data.loc[row, 'Frame'])
                        tracking_off += 1
                    else:
                        continue
            tracking_sync['TTL input stop'] = tracking_data.iloc[-1, 0]

            # save data to sync_dict
            sync_dict['tracking'] = tracking_sync
            print('Completed! There are {} total LED events in the tracking file.'.format(len(tracking_sync.keys()) - 2))

            # save the tracking .csv with everything before the first LEDon and after the last LEDon removed
            with open('{}_interpolated.csv'.format(track_file[:-4]), 'r') as inp, open('{}final.csv'.format('{}_interpolated.csv'.format(track_file[:-4])[:-16]), 'w', newline='') as out:
                writer = csv.writer(out)
                for rowindx, row in enumerate(csv.reader(inp)):
                    if rowindx < 7 or (all_led_frames[0]+7) <= rowindx <= (all_led_frames[-1]+7):
                        writer.writerow(row)

        else:
            print('Tracking file not given or found.')

        # extract LED events from the IMU file
        if os.path.exists(imu_file):

            print('Extracting sync data from the IMU file, please remain patient.')

            # load the files as df, add the imu header and get LED times
            imu_df = pd.read_csv('{}'.format(imu_file), sep=',', header=None)
            imu_df.columns = ['loop.starttime (ms)', 'sample.time (ms)', 'acc.x', 'acc.y', 'acc.z',  'linacc.x', 'linacc.y', 'linacc.z', 'gyr.x', 'gyr.y', 'gyr.z',
                              'mag.x', 'mag.y', 'mag.z', 'euler.x', 'euler.y', 'euler.z', 'LED', 'sound', 'sys.cal', 'gyr.cal', 'acc.cal', 'mag.cal']

            sample_array = imu_df['sample.time (ms)'].tolist()
            led_array = imu_df['LED'].tolist()
            teensy_time = {}
            imu_led = {}
            imu_on = 1
            imu_off = 1
            for indx, item in tqdm(enumerate(led_array)):
                if item != 0 and led_array[indx - 1] == 0:
                    imu_led['{}LEDon'.format(imu_on)] = indx
                    teensy_time['{}LEDon'.format(imu_on)] = sample_array[indx]
                    imu_on += 1
                elif item == 0 and led_array[indx - 1] != 0:
                    if ledoff:
                        imu_led['{}LEDoff'.format(imu_off)] = indx
                        teensy_time['{}LEDoff'.format(imu_off)] = sample_array[indx]
                        imu_off += 1
                    else:
                        continue

            # save data to sync_dict
            sync_dict['imu_frame_number'] = imu_led
            sync_dict['teensy_sample_time'] = teensy_time
            print('Completed! here are {} total LED events in the IMU file.'.format(len(imu_led.keys())))

            # save IMU data to file
            if type(imu_pkl) == str:
                with open(imu_pkl, 'wb') as imu_data:
                    pickle.dump(imu_df, imu_data)

        else:
            print('IMU file not given or found.')

        # pack sync_dict into a df and save it to a .pkl file
        choose_imec = 'imec{}'.format(ground_probe)
        export_sync_df = pd.DataFrame(index=sync_dict[choose_imec].keys(), columns=sync_dict.keys())
        for data_key in sync_dict.keys():
            for event_key in sync_dict[choose_imec].keys():
                if event_key in sync_dict[data_key].keys():
                    export_sync_df.loc[event_key, data_key] = sync_dict[data_key][event_key]

        # check if dataframe contains NANs
        if export_sync_df.iloc[2:-2, :].isnull().values.any():
            print('There is at least one NAN value as a LED event in the saved sync_df! Check it out!')

        with open('{}'.format(self.sync_df), 'wb') as df:
            pickle.dump(export_sync_df, df)

        print('Extraction complete! It took {:.2f} minutes.'.format((time.time() - t) / 60))