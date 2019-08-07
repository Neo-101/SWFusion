#!/usr/bin/python
#encoding:utf-8

import urllib
import urllib.request
import os
import numpy as np

qscat_year_list = ['2000', '2001', '2002', '2003', '2004', '2005', '2006', '2007', '2008']
ascat_year_list = ['2008', '2009', '2010', '2011', '2012', '2013', '2014', '2015', '2016', '2017']
windsat_year_list = ['2004', '2005', '2006', '2007', '2008', '2009', '2010', '2011', '2012', '2013', '2014', '2015', '2016', '2017']
month_list = ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12']
qscat_miss = ['20001117', '20010512', '20010513', '20011118', '20020320']
ascat_miss = []
windsat_miss = ['20040128', '20040130', '20040301', '20040302', '20040303', '20040430']

def Schedule(a,b,c):
    """
        a:已经下载的数据块
        b:数据块的大小
        c:远程文件的大小
    """
    per = 100.0 * a * b / c
    if per > 100 :
        per = 100
    print('%.2f%%' % per)

def get_data(type, middle, suffix, year, miss):
    print('get_data' + type + year)
    for month in month_list:
        if month == '02':
            days = 28
        elif month == '04' or month == '06' or month == '09' or month == '11':
            days = 30
        else:
            days = 31
        for i in range(days):
            if i <= 8:
                filename = year + month + '0' + str(i + 1)
            else:
                filename = year + month + str(i + 1)
            if filename in miss:
                continue
            local = './datasets/' + type + '/' + year + '/' + filename + '.gz'
            if os.path.exists(local):
                continue
            url = 'http://data.remss.com/'+type+middle+'/y'+year+'/m'+month+'/'+type+'_' + filename+suffix
            print(url)
            urllib.request.urlretrieve(url, local, Schedule)

def get_data_for_windsat(type, middle, suffix, year, miss):
    print('get_data' + type + year)
    for month in month_list:
        if month == '02':
            days = 29
        elif month == '04' or month == '06' or month == '09' or month == '11':
            days = 30
        else:
            days = 31
        for i in range(days):
            if i <= 8:
                filename = year + month + '0' + str(i+1)
            else:
                filename = year + month + str(i+1)
            if filename in miss:
                continue
            local = './datasets/' + type+'/'+year + '/' + filename + '.gz'
            if os.path.exists(local):
                continue
            url = 'http://data.remss.com/'+type+middle+'/y'+year+'/m'+month+'/wsat_' + filename+suffix
            write_information('./'+year+'.txt', url+'\n')
            # urllib.request.urlretrieve(url, local, Schedule)

def write_information(filename, data):
    with open(filename, 'a') as f:
        f.write(data)

if __name__ == '__main__':

    type = 'windsat'
    middle = '/bmaps_v07.0.1'
    suffix = 'v7.0.1.gz'
    miss = windsat_miss
    for year in windsat_year_list:
        get_data_for_windsat(type, middle, suffix, year, miss)