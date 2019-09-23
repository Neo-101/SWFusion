import datetime
import logging
import os
import time

import cdsapi
import pygrib
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Integer, Float, String, DateTime
from sqlalchemy import Column
from sqlalchemy.schema import Table, MetaData
from sqlalchemy.orm import mapper

import ibtracs
import utils

Base = declarative_base()

class ERA5Manager(object):
    def __init__(self, CONFIG, period, region, passwd):
        self.CONFIG = CONFIG
        self.period = period
        self.region = region
        self.db_root_passwd = passwd
        self.engine = None
        self.session = None

        self.logger = logging.getLogger(__name__)

        self.years = [x for x in range(self.period[0].year,
                                       self.period[1].year+1)]
        self.main_hours = self.CONFIG['era5']['main_hours']
        self.edge = self.CONFIG['era5']['subset_edge_in_degree']

        self.cdsapi_client = cdsapi.Client()
        self.vars = self.CONFIG['era5']['vars']
        self.pres_lvl = self.CONFIG['era5']['pres_lvl']

        self.spa_resolu = self.CONFIG['era5']['spatial_resolution']
        self.lat_grid_points = [y * self.spa_resolu - 90 for y in range(
            self.CONFIG['era5']['lat_grid_points_number'])]
        self.lon_grid_points = [x * self.spa_resolu - 90 for x in range(
            self.CONFIG['era5']['lon_grid_points_number'])]
        # Size of 3D grid points around TC center
        self.grid_3d = dict()
        self.grid_3d['length'] = self.grid_3d['width'] = int(
            self.edge/self.spa_resolu + 1)
        self.grid_3d['height'] = len(self.pres_lvl)

        utils.setup_database(self, Base)
        # self.download_and_read()
        self.read('/Users/lujingze/Downloads/download.grib')

    def get_era5_table_class(self, sid, dt, lat_index, lon_index):
        dt_str = dt.strftime('%Y%m%d%H%M%S')
        table_name = f'era5_tc_{sid}_{dt_str}_{lat_index}_{lon_index}'

        class ERA5(object):
            pass

        if self.engine.dialect.has_table(self.engine, table_name):
            metadata = MetaData(bind=self.engine, reflect=True)
            t = metadata.tables[table_name]
            mapper(ERA5, t)

            return table_name, None, ERA5

        cols = []
        cols.append(Column('key', Integer, primary_key=True))
        cols.append(Column('x', Integer, nullable=False))
        cols.append(Column('y', Integer, nullable=False))
        cols.append(Column('z', Integer, nullable=False))
        cols.append(Column('lat', Float, nullable=False))
        cols.append(Column('lon', Float, nullable=False))
        cols.append(Column('pres_lvl', Integer, nullable=False))
        for var in self.vars:
            cols.append(Column(var, Float))
        cols.append(Column('x_y_z', String(20), nullable=False,
                           unique=True))

        metadata = MetaData(bind=self.engine)
        t = Table(table_name, metadata, *cols)
        mapper(ERA5, t)

        return table_name, t, ERA5

    def download_majority(self, file_path, year, month):
        self.cdsapi_client.retrieve(
            'reanalysis-era5-pressure-levels',
            {
                'product_type':'reanalysis',
                'format':'grib',
                'variable':self.vars,
                'pressure_level':self.pres_lvl,
                'year':f'{year}',
                'month':str(month).zfill(2),
                'day':list(self.dt_majority[year][month]['day']),
                'time':list(self.dt_majority[year][month]['time'])
            },
            file_path)

    def download_minority(self, file_path, year, month, day_str):
        self.cdsapi_client.retrieve(
            'reanalysis-era5-pressure-levels',
            {
                'product_type':'reanalysis',
                'format':'grib',
                'variable':self.vars,
                'pressure_level':self.pres_lvl,
                'year':f'{year}',
                'month':str(month).zfill(2),
                'day':day_str,
                'time':list(self.dt_minority[year][month][day_str])
            },
            file_path)

    def download_and_read(self):
        self.logger.info('Downloading and reading ERA5 reanalysis')
        self._get_target_datetime()
        majority_dir = self.CONFIG['era5']['dirs']['rea_pres_lvl_maj']
        minority_dir = self.CONFIG['era5']['dirs']['rea_pres_lvl_min']
        os.makedirs(majority_dir, exist_ok=True)
        os.makedirs(minority_dir, exist_ok=True)

        for year in self.dt_majority.keys():
            for month in self.dt_majority[year].keys():
                file_path = f'{majority_dir}{year}{str(month).zfill(2)}.grib'
                self.logger.info(f'Downloading majority {file_path}')
                if not os.path.exists(file_path):
                    self.download_majority(file_path, year, month)
                self.logger.info(f'Reading majority {file_path}')
                self.read(file_path)
                os.remove(file_path)

        for year in self.dt_minority.keys():
            for month in self.dt_minority[year].keys():
                for day_str in self.dt_minority[year][month].keys():
                    file_path =\
                            (f'{minority_dir}{year}{str(month).zfill(2)}'
                             + f'{day_str}.grib')
                    self.logger.info(f'Downloading minority {file_path}')
                    if not os.path.exists(file_path):
                        self.download_minority(file_path, year, month, day_str)
                    self.logger.info(f'Reading minority {file_path}')
                    self.read(file_path)
                    os.remove(file_path)

    def read(self, file_path):
        # load grib file
        grbs = pygrib.open(file_path)
        # Alter TC table
        tc_table_name = self.CONFIG['ibtracs']['table_name']
        # loop TC table
        TCTable = utils.get_class_by_tablename(self.engine,
                                               tc_table_name)
        tc_query = self.session.query(TCTable)
        total = tc_query.count()
        del tc_query
        count = 0
        info = f'Reading reanalysis data of TC records'
        self.logger.info(info)
        # get lat and lon of row
        for row in self.session.query(TCTable).yield_per(
            self.CONFIG['database']['batch_size']['query']):

            # Get range of matching cell
            hit, lat1, lat2, lon1, lon2 = \
                    self._get_subset_range_of_grib(row.lat, row.lon)
            if not hit:
                continue

            tc_datetime = row.datetime
            count += 1
            print(f'\r{info} {count}/{total}', end='')

            lat_index, lon_index = \
                    self._get_latlon_index_of_closest_grib_point(
                        row.lat, row.lon)

            table_name, sa_table, ERA5Table = self.get_era5_table_class(
                row.sid, tc_datetime, lat_index, lon_index)
            era5_table_entity = self._gen_whole_era5_table_entity(
                ERA5Table, lat1, lat2, lon1, lon2)
            read_hit_count = 0

            # read out variables
            for m in range(grbs.messages):
                grb = grbs.message(m+1)
                grb_date, grb_time = str(grb.dataDate), str(grb.dataTime)
                if grb_time == '0':
                    grb_time = '000'
                grb_datetime = datetime.datetime.strptime(
                    f'{grb_date}{grb_time}', '%Y%m%d%H%M%S')
                if tc_datetime != grb_datetime:
                    continue

                # extract corresponding data matrix in ERA5 reanalysis file
                read_hit = self._read_data_matrix(era5_table_entity, grb,
                                                  lat1, lat2, lon1, lon2)
                if read_hit:
                    read_hit_count += 1
            if not read_hit_count:
                continue
            if sa_table is not None:
                # Create table of ERA5 data cube
                sa_table.create(self.engine)
                self.session.commit()
            # write into DB
            start = time.process_time()
            utils.bulk_insert_avoid_duplicate_unique(
                era5_table_entity,
                int(self.CONFIG['database']['batch_size']['insert']/10),
                ERA5Table, ['x_y_z'], self.session,
                check_self=True)
            end = time.process_time()

            self.logger.debug((f'Bulk inserting ERA5 data into '
                               + f'{table_name} in {end-start:2f} s'))
        utils.delete_last_lines()
        print('Done')

    def _gen_whole_era5_table_entity(self, ERA5Table,
                                     lat1, lat2, lon1, lon2):
        entity = []

        for x in range(self.grid_3d['length']):
            for y in range(self.grid_3d['width']):
                for z in range(self.grid_3d['height']):
                    pt = ERA5Table()
                    pt.x, pt.y, pt.z = x, y, z
                    pt.x_y_z = f'{x}_{y}_{z}'

                    pt.lat = lat1 + x * self.spa_resolu
                    pt.lon = (lon1 + y * self.spa_resolu) % 360
                    pt.pres_lvl = int(self.pres_lvl[z])

                    entity.append(pt)

        return entity

    def _read_data_matrix(self, era5, grb, lat1, lat2, lon1, lon2):
        data, lats, lons = grb.data(lat1, lat2, lon1, lon2)

        # CHECK whether data.shape is [lat, lon] or [lon, lat]
        name = grb.name.replace(" ", "_").lower()
        z = self.pres_lvl.index(str(grb.level))
        hit_count = 0

        for x in range(self.grid_3d['length']):
            for y in range(self.grid_3d['width']):
                value = utils.convert_dtype(data[x][y])
                if value == grb.missingValue:
                    continue
                hit_count += 1
                index = (x * self.grid_3d['width'] * self.grid_3d['height']
                         + y * self.grid_3d['height']
                         + z)
                setattr(era5[index], name, value)

        if hit_count:
            return True
        else:
            return False

    def _get_subset_range_of_grib_point(self, lat, lon):
        lat_ae = [abs(lat-y) for y in self.lat_grid_points]
        lon_ae = [abs(lon-x) for x in self.lon_grid_points]

        lat_match = self.lat_grid_points[lat_ae.index(min(lat_ae))]
        lon_match = self.lon_grid_points[lon_ae.index(min(lon_ae))]

        lat1 = lat_match if lat > lat_match else lat
        lat2 = lat_match if lat < lat_match else lat
        lon1 = lon_match if lon > lon_match else lon
        lon2 = lon_match if lon < lon_match else lon

        return lat1, lat2, lon1, lon2

    def _get_latlon_index_of_closest_grib_point(self, lat, lon):
        lat_ae = [abs(lat-y) for y in self.lat_grid_points]
        lon_ae = [abs(lon-x) for x in self.lon_grid_points]

        lat_match_index = lat_ae.index(min(lat_ae))
        lon_match_index = lon_ae.index(min(lon_ae))

        return lat_match_index, lon_match_index

    def _get_subset_range_of_grib(self, lat, lon):
        lat_ae = [abs(lat-y) for y in self.lat_grid_points]
        lon_ae = [abs(lon-x) for x in self.lon_grid_points]

        lat_match = self.lat_grid_points[lat_ae.index(min(lat_ae))]
        lon_match = self.lon_grid_points[lon_ae.index(min(lon_ae))]

        half_edge = self.edge / 2

        if lat_match - half_edge < -90 or lat_match + half_edge > 90 :
            return False, 0, 0, 0, 0

        lat1 = lat_match - half_edge
        lat2 = lat_match + half_edge
        lon1 = (lon_match - half_edge + 360) % 360
        lon2 = (lon_match + half_edge) % 360

        return True, lat1, lat2, lon1, lon2

    def _update_majority_datetime_dict(self, dt_dict, year, month, day, hour):
        day_str = str(day).zfill(2)
        time_str = f'{str(hour).zfill(2)}:00'

        if year not in dt_dict:
            dt_dict[year] = dict()
        if month not in dt_dict[year]:
            dt_dict[year][month] = dict()
            dt_dict[year][month]['day'] = set()
            dt_dict[year][month]['time'] = set()

        dt_dict[year][month]['day'].add(day_str)
        dt_dict[year][month]['time'].add(time_str)

    def _update_minority_datetime_dict(self, dt_dict, year, month, day, hour):
        day_str = str(day).zfill(2)
        time_str = f'{str(hour).zfill(2)}:00'

        if year not in dt_dict:
            dt_dict[year] = dict()
        if month not in dt_dict[year]:
            dt_dict[year][month] = dict()
        if day_str not in dt_dict[year][month]:
            dt_dict[year][month][day_str] = set()

        dt_dict[year][month][day_str].add(time_str)

    def _get_target_datetime(self):
        tc_table_name = self.CONFIG['ibtracs']['table_name']
        TCTable = utils.get_class_by_tablename(self.engine,
                                               tc_table_name)
        dt_majority = dict()
        dt_minority = dict()

        for row in self.session.query(TCTable).yield_per(
            self.CONFIG['database']['batch_size']['query']):

            year, month = row.datetime.year, row.datetime.month
            day, hour = row.datetime.day, row.datetime.hour
            if hour in self.main_hours:
                self._update_majority_datetime_dict(dt_majority, year,
                                                    month, day, hour)
            else:
                self._update_minority_datetime_dict(dt_minority, year,
                                                    month, day, hour)

        self.dt_majority = dt_majority
        self.dt_minority = dt_minority
