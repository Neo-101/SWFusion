"""Manage downloading and reading NOAA HRD SFMR hurricane data.

"""
import datetime
import gzip
import logging
import math
import pickle
import re
import os
import time

from bs4 import BeautifulSoup
import mysql.connector
import numpy as np
import pandas as pd
import requests
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, Float, String, DateTime, Date
from sqlalchemy.orm import sessionmaker
from sqlalchemy import tuple_
from sqlalchemy.schema import Table

import utils
import netcdf_util

MASKED = np.ma.core.masked
Base = declarative_base()
DynamicBase = declarative_base(class_registry=dict())

class HurrSfmr(Base):
    __tablename__ = 'hurr_sfmr_record'

    key = Column(Integer, primary_key=True)
    year = Column(Integer, nullable=False)
    name = Column(String(length=20), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    min_lat = Column(Float, nullable=False)
    max_lat = Column(Float, nullable=False)
    min_lon = Column(Float, nullable=False)
    max_lon = Column(Float, nullable=False)
    name_year = Column(String(20), nullable=False, unique=True)

class SfmrManager(object):

    def __init__(self, CONFIG, period, region, passwd):
        self.SFMR_CONFIG = CONFIG['sfmr']
        self.CONFIG = CONFIG
        self.period = period
        self.region = region
        self.db_root_passwd = passwd
        self.engine = None
        self.session = None

        self.years = [x for x in range(self.period[0].year,
                                       self.period[1].year+1)]
        self._gen_all_year_hurr()
        self._extract_year_hurr()

        self.logger = logging.getLogger(__name__)

        # self.download()

        utils.reset_signal_handler()
        utils.setup_database(self, Base)
        self.read()

    def download(self):
        """Download SFMR data of hurricanes from NOAA HRD.

        """
        correct, period = utils.check_and_update_period(
            self.period, self.SFMR_CONFIG['period_limit'],
            self.CONFIG['workflow']['prompt'])
        if not correct:
            return
        utils.setup_signal_handler()
        self._download_sfmr_data()

    def read(self, read_all=False):
        """Read data into MySQL database.

        """
        self._insert_all_hurr_record()
        # self._insert_sfmr(read_all)

        return

    def _insert_all_hurr_record(self):
        self.logger.info(
            self.SFMR_CONFIG['prompt']['info']['read_hurr_record'])

        data_root_dir = self.SFMR_CONFIG['dirs']['hurr']
        all_hurr_records = []

        for year in self.all_year_hurr.keys():
            for hurr in self.all_year_hurr[year]:
                spec_data_dir = '{0}{1}/{2}/'.format(
                    data_root_dir, year, hurr)
                try:
                    file_names = [f for f in os.listdir(spec_data_dir)
                                  if f.endswith('.nc')]
                except FileNotFoundError:
                    continue
                if not len(file_names):
                    continue

                start_date = datetime.date(9999, 12, 31)
                end_date = datetime.date(1, 1, 1)
                for file in file_names:
                    date_ = datetime.datetime.strptime(
                        file.split('SFMR')[1][:8]+'000000',
                        '%Y%m%d%H%M%S').date()

                    if date_ < start_date:
                        start_date = date_
                    if date_ > end_date:
                        end_date = date_

                hurr_record = HurrSfmr()
                hurr_record.year = year
                hurr_record.name = hurr
                hurr_record.start_date = start_date
                hurr_record.end_date = end_date
                hurr_record.min_lat = 90.0
                hurr_record.max_lat = -90.0
                hurr_record.min_lon = 360.0
                hurr_record.max_lon = 0.0
                hurr_record.name_year = f'{hurr}{year}'

                all_hurr_records.append(hurr_record)

        start = time.process_time()
        utils.bulk_insert_avoid_duplicate_unique(
            all_hurr_records,
            self.CONFIG['database']['batch_size']['insert'],
            HurrSfmr, ['name_year'], self.session, check_self=True)
        end = time.process_time()

        self.logger.debug((f'Bulk inserting general hurricane data into '
                           + f'{HurrSfmr.__tablename__} '
                           + f'in {end-start:.2f} s'))

    def _extract_year_hurr_file_path(self, read_all=False):
        data_root_dir = self.SFMR_CONFIG['dirs']['hurr']
        self.year_hurr_file_path = dict()

        for year in self.year_hurr.keys():
            if not len(self.year_hurr[year]):
                continue
            self.year_hurr_file_path[year] = dict()

            for hurr in self.year_hurr[year]:
                spec_data_dir = '{0}{1}/{2}/'.format(
                    data_root_dir, year, hurr)
                try:
                    file_names = [f for f in os.listdir(spec_data_dir)
                                  if f.endswith('.nc')]
                except FileNotFoundError:
                    pass
                if not len(file_names):
                    continue

                self.year_hurr_file_path[year][hurr] = []

                for file in file_names:
                    date_ = datetime.datetime.strptime(
                        file.split('SFMR')[1][:8]+'000000',
                        '%Y%m%d%H%M%S').date()

                    if not read_all and utils.check_period(
                        date_, self.period):
                        self.year_hurr_file_path[year][hurr].append(
                            spec_data_dir + file)
                    if read_all:
                        self.year_hurr_file_path[year][hurr].append(
                            spec_data_dir + file)

    def _insert_sfmr(self, read_all=False):
        self.logger.info(
            self.SFMR_CONFIG['prompt']['info']['read_hurr_sfmr'])
        self._extract_year_hurr_file_path(read_all)
        # Create SFMR table
        table_name_prefix = self.SFMR_CONFIG['table_names']['prefix']
        skip_vars = ['DATE', 'TIME']
        notnull_vars = ['LAT', 'LON', 'SRR', 'SWS']
        unique_vars = []
        custom_cols = {1: Column('DATETIME', DateTime(),
                                 nullable=False, unique=False),
                       21: Column('SPACE_TIME', String(255),
                                 nullable=False, unique=True)}
        total = 0
        for year in self.year_hurr_file_path.keys():
            for hurr in self.year_hurr_file_path[year].keys():
                total += len(self.year_hurr_file_path[year][hurr])
        count = 0

        for year in self.year_hurr_file_path.keys():
            for hurr in self.year_hurr_file_path[year].keys():
                if not len(self.year_hurr_file_path[year][hurr]):
                    continue

                table_name = f'{table_name_prefix}{year}_{hurr}'
                nc_template_path = self.year_hurr_file_path\
                        [year][hurr][0]
                SfmrTable = utils.create_table_from_netcdf(
                    self.engine, nc_template_path,
                    table_name, self.session, skip_vars,
                    notnull_vars, unique_vars, custom_cols)

                for file_path in self.year_hurr_file_path[year][hurr]:
                    count += 1
                    info = (f'Extracting SFMR data from '
                            + f'{file_path.split("/")[-1]}')
                    if count > 1:
                        utils.delete_last_lines()
                    print(f'\r{info} ({count}/{total})', end='')

                    start = time.process_time()
                    one_day_records, min_lat, max_lat,\
                            min_lon, max_lon = \
                            self._extract_sfmr_from_netcdf(file_path,
                                                           SfmrTable)
                    end = time.process_time()
                    self.logger.debug(f'{info} in {end-start:.2f} s')

                    start = time.process_time()
                    utils.bulk_insert_avoid_duplicate_unique(
                        one_day_records,
                        self.CONFIG['database']['batch_size']['insert'],
                        SfmrTable, ['SPACE_TIME'], self.session,
                        check_self=True)
                    end = time.process_time()
                    self.logger.debug(
                        (f'Bulk inserting sfmr data into {table_name} '
                         + f'in {end-start:.2f} s'))
                    # Update SFMR records of hurricanes
                    date_ = datetime.datetime.strptime(
                        file_path.split('/')[-1].\
                        split('SFMR')[1][:8]+'000000',
                        '%Y%m%d%H%M%S').date()
                    self._update_hurr_record(hurr, date_, min_lat,
                                             max_lat, min_lon,
                                             max_lon)
        utils.delete_last_lines()
        print('Done')

    def _update_hurr_record(self, hurr_name, date_, min_lat, max_lat,
                            min_lon, max_lon):
        start = time.process_time()
        hurr_query = self.session.query(HurrSfmr).\
                filter(HurrSfmr.name == hurr_name).\
                filter(HurrSfmr.start_date <= date_).\
                filter(HurrSfmr.end_date >= date_)

        if hurr_query.count() > 1:
            self.logger.exception(
                ('Column "name_year" is not unique'))
        target = hurr_query.first()

        if min_lat < target.min_lat:
            target.min_lat = min_lat
        if max_lat > target.max_lat:
            target.max_lat = max_lat
        if min_lon < target.min_lon:
            target.min_lon = min_lon 
        if max_lon > target.max_lon:
            target.max_lon = max_lon

        self.session.commit()
        end = time.process_time()

        self.logger.debug((f'Updating columns about region of '
                           + f'{HurrSfmr.__tablename__} '
                           + f'in {end-start:.2f} s'))

    def _extract_sfmr_from_netcdf(self, file_path, SfmrTable):
        """Dump one SFMR NetCDF file into one pickle file.

        """
        nc_file = file_path
        table_class = SfmrTable
        skip_vars = ['DATE', 'TIME']
        datetime_func = datetime_from_netcdf
        datetime_col_name = 'DATETIME'
        missing = MASKED
        valid_func = valid_netcdf
        unique_func = utils.gen_space_time_fingerprint
        unique_col_name = 'SPACE_TIME'
        lat_name = 'LAT'
        lon_name = 'LON'
        period = self.period
        region = self.region
        not_null_vars = ['LAT', 'LON', 'SWS', 'SRR']

        res, min_lat, max_lat, min_lon, max_lon = \
                utils.extract_netcdf_to_table(
                    nc_file, table_class, skip_vars, datetime_func,
                    datetime_col_name, missing, valid_func, unique_func,
                    unique_col_name, lat_name, lon_name, period, region,
                    not_null_vars)

        return res, min_lat, max_lat, min_lon, max_lon

    def _download_sfmr_data(self):
        """Download SFMR data of hurricanes.

        Parameters
        ----------
        None
            Nothing is required by this function.

        Returns
        -------
        hit_times : dict
            Times of hurricane NetCDF file's date being in period.

        """
        self.logger.info(self.SFMR_CONFIG['prompt']['info']\
                         ['download_hurr'])
        utils.set_format_custom_text(
            self.SFMR_CONFIG['data_name_length'])
        suffix = '.nc'
        save_root_dir = self.SFMR_CONFIG['dirs']['hurr']
        os.makedirs(save_root_dir, exist_ok=True)

        total = 0
        count = 0
        for year in self.year_hurr.keys():
            total += len(self.year_hurr[year])

        for year in self.year_hurr.keys():
            hurrs = list(self.year_hurr[year])
            for hurr in hurrs:
                count += 1
                info = (f'Download SFMR data of hurricane {hurr} '
                        + f'in {year}')
                self.logger.debug(info)
                if count > 1:
                    utils.delete_last_lines()
                print(f'\r{info} ({count}/{total})', end='')

                # Create directory to store SFMR files
                dir_path = f'{save_root_dir}{year}/{hurr}/'
                os.makedirs(dir_path, exist_ok=True)
                # Generate keyword to consist url
                keyword = f'{hurr}{year}'
                url = (f'{self.SFMR_CONFIG["urls"]["prefix"]}'
                       + f'{keyword}'
                       + f'{self.SFMR_CONFIG["urls"]["suffix"]}')
                # Get page according to url
                page = requests.get(url)
                data = page.text
                soup = BeautifulSoup(data, features='lxml')
                anchors = soup.find_all('a')

                # Times of NetCDF file's date being in period
                for link in anchors:
                    href = link.get('href')
                    # Find href of netcdf file
                    if href.endswith(suffix):
                        # Extract file name
                        file_name = href.split('/')[-1]
                        tail_half = file_name.split('SFMR')[1]
                        try:
                            # There may be NetCDF name format
                            # like 'USAF_SFMR0809221638.nc'
                            # from 'https://www.aoml.noaa.gov/hrd'
                            # '/Storm_pages/kyle2008/sfmr.html'
                            # It is very annoying and there seems
                            # no simple rule to check this problem.
                            # Because it hard to distinguish
                            # 'SFMR20110536' and 'SFMR20110524'.
                            # First one is the case as kyle2008, its
                            # actually date is 2020/11/05.
                            # Second one is a normal case, its
                            # actually date is 2011/05/24.
                            # Before 2020, following rule may work.
                            if (tail_half.startswith('20')
                                or tail_half.startswith('199')):
                                date_str = tail_half[:8]
                                date_ = datetime.date(int(date_str[:4]),
                                                int(date_str[4:6]),
                                                int(date_str[6:]))
                            else:
                                date_str = tail_half[:6]
                                date_ = datetime.date(
                                    int(f'20{date_str[:2]}'),
                                    int(date_str[2:4]),
                                    int(date_str[4:]))
                                file_name = (
                                    f'{file_name.split("SFMR")[0]}SFMR20'
                                    + f'{file_name.split("SFMR")[1]}')
                        except Exception as msg:
                            breakpoint()
                            exit(msg)
                        if not utils.check_period(date_, self.period):
                            continue
                        file_path = dir_path + file_name

                        utils.download(href, file_path)

        utils.delete_last_lines()
        print('Done')

    def _gen_all_year_hurr(self):
        this_year = datetime.datetime.today().year
        lastest_year = True

        if os.path.exists(
            self.SFMR_CONFIG['vars_path']['all_year_hurr']):
            if datetime.datetime.fromtimestamp(os.path.getmtime(
                self.SFMR_CONFIG['vars_path']['all_year_hurr'])
            ).year < this_year:
                latest_year = False

            if lastest_year:
                with open(self.SFMR_CONFIG['vars_path']\
                          ['all_year_hurr'], 'rb') as file:
                    self.all_year_hurr = pickle.load(file)

                return

        self.all_year_hurr = {}

        start_year = self.SFMR_CONFIG['period_limit']['start'].year
        end_year = self.SFMR_CONFIG['period_limit']['end'].year
        if this_year < end_year:
            end_year = this_year

        for year in range(start_year, end_year+1):
            info = f'Finding hurricanes of year {year}'
            self.logger.debug(info)
            print(f'\r{info}', end='')

            if year < 1994:
                year = 'prior1994'
            if year == this_year:
                year = ''
            url = (f'{self.SFMR_CONFIG["urls"]["hurricane"][:-5]}'
                   + f'{year}.html')
            page = requests.get(url)
            data = page.text
            soup = BeautifulSoup(data, features='lxml')
            anchors = soup.find_all('a')

            self.all_year_hurr[year] = set()

            for link in anchors:
                if not link.contents:
                    continue
                text = link.contents[0]
                if text != 'SFMR':
                    continue
                href = link.get('href')
                hurr = href.split('/')[-2][:-4]
                self.all_year_hurr[year].add(hurr)
        utils.delete_last_lines()
        print('Done')

        utils.save_relation(
            self.SFMR_CONFIG['vars_path']['all_year_hurr'],
            self.all_year_hurr)

    def _extract_year_hurr(self):
        self.year_hurr = {}

        for year in self.years:
            self.year_hurr[year] = self.all_year_hurr[year]

def valid_netcdf(vars, index):
    if vars['FLAG'][index]:
        return False
    return True

def datetime_from_netcdf(vars, index, missing):
    """
    Note
    ----
    Only supports 'if var is missing' check now. Have not supported
    'if var == missing' check.

    """
    DATE, TIME = str(vars['DATE'][index]), str(vars['TIME'][index])
    # TIME variable's valid range is [0, 235959]
    if len(TIME) < 6:
        TIME = '0' * (6 - len(TIME)) + TIME
    if DATE is missing or TIME is missing:
        return False
    datetime_ = datetime.datetime.strptime(DATE + TIME, '%Y%m%d%H%M%S')

    return datetime_

