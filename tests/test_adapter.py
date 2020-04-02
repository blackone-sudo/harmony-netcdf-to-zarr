"""
Tests the Harmony adapter, including end-to-end tests of Harmony CLI invocations
"""

import unittest
from unittest.mock import patch
from moto import mock_s3

import boto3
import os
import s3fs
import zarr
import textwrap
import argparse
import tempfile

from .util.file_creation import create_full_dataset, ROOT_METADATA_VALUES
from .util.harmony_interaction import MOCK_ENV, mock_message_for, parse_callbacks

from harmony_netcdf_to_zarr.__main__ import main
from harmony_netcdf_to_zarr.adapter import NetCDFToZarrAdapter
from harmony.message import Message

class TestAdapter(unittest.TestCase):
    """
    Tests the Harmony adapter
    """

    @patch.dict(os.environ, MOCK_ENV)
    @patch.object(NetCDFToZarrAdapter, '_callback_post')
    @mock_s3
    def test_end_to_end_file_conversion(self, _callback_post):
        """
        Full end-to-end test of the adapter from call to `main` to Harmony callbacks, including
        ensuring the contents of the file are correct.  Mocks S3 interactions using @mock_s3.
        """
        conn = boto3.resource('s3', region_name='us-west-2')
        conn.create_bucket(Bucket='example-bucket')

        netcdf_file = create_full_dataset()
        netcdf_file2 = create_full_dataset()
        try:
            message = mock_message_for(netcdf_file, netcdf_file2)
            main(['harmony_netcdf_to_zarr', '--harmony-action', 'invoke', '--harmony-input', message])
        finally:
            os.remove(netcdf_file)
            os.remove(netcdf_file2)

        callbacks = parse_callbacks(_callback_post)

        # -- Progress and Callback Assertions --
        # Assert that we got three callbacks, one for first file, one for the second, and the final message
        self.assertEqual(len(callbacks), 3)
        self.assertEqual(callbacks[0]['progress'], '50')
        self.assertEqual(callbacks[0]['item[type]'], 'application/x-zarr')
        self.assertEqual(callbacks[1]['progress'], '100')
        self.assertEqual(callbacks[1]['item[type]'], 'application/x-zarr')
        self.assertEqual(callbacks[2], {'status': 'successful'})
        self.assertNotEqual(callbacks[0]['item[href]'], callbacks[1]['item[href]'])
        self.assertTrue(callbacks[0]['item[href]'].endswith('.zarr'))
        self.assertTrue(callbacks[1]['item[href]'].endswith('.zarr'))


        # Open the Zarr file that the adapter called back with
        zarr_location = callbacks[0]['item[href]']
        store = s3fs.S3FileSystem().get_mapper(root=zarr_location, check=False)
        out = zarr.open_consolidated(store)

        # -- Hierarchical Structure Assertions --
        contents = textwrap.dedent("""
            /
             ├── data
             │   ├── horizontal
             │   │   ├── east (1, 3, 3) uint8
             │   │   └── west (1, 3, 3) uint8
             │   └── vertical
             │       ├── north (1, 3, 3) uint8
             │       └── south (1, 3, 3) uint8
             ├── location
             │   ├── lat (3, 3) float32
             │   └── lon (3, 3) float32
             └── time (1,) int32
            """).strip()
        self.assertEqual(str(out.tree()), contents)

        # -- Metadata Assertions --
        # Root level values
        self.assertEqual(dict(out.attrs), ROOT_METADATA_VALUES)

        # Group metadata
        self.assertEqual(out['data'].attrs['description'], 'Group to hold the data')

        # Variable metadata
        var = out['data/vertical/north']
        self.assertEqual(var.attrs['coordinates'], 'lon lat')

        # -- Data Assertions --
        # Nested Byte Arrays
        self.assertEqual(out['data/vertical/north'][0, 0, 2], 16)
        self.assertEqual(out['data/vertical/north'][0, 2, 0], 0)
        self.assertEqual(out['data/vertical/south'][0, 2, 0], 16)
        self.assertEqual(out['data/vertical/south'][0, 0, 2], 0)
        self.assertEqual(out['data/horizontal/east'][0, 2, 2], 16)
        self.assertEqual(out['data/horizontal/east'][0, 0, 0], 0)
        self.assertEqual(out['data/horizontal/west'][0, 0, 0], 16)
        self.assertEqual(out['data/horizontal/west'][0, 2, 2], 0)

        # 2D Nested Float Arrays
        self.assertEqual(out['location/lat'][0, 1],  5.5)
        self.assertEqual(out['location/lon'][0, 1], -5.5)

        # 1D Root-Level Float Array sharing its name with a dimension
        self.assertEqual(out['time'][0],  166536)

    @patch.object(argparse.ArgumentParser, 'error', return_value=None)
    def test_does_not_accept_non_harmony_clis(self, argparse_error):
        """
        Tests that when a non-Harmony argument gets passed, the CLI returns an error
        """
        main(['harmony_netcdf_to_zarr', '--discord'])
        argparse_error.assert_called_with('Only --harmony CLIs are supported')

    @patch.dict(os.environ, dict(USE_LOCALSTACK='true', LOCALSTACK_HOST='fake-host'))
    def test_localstack_client(self):
        """
        Tests that when USE_LOCALSTACK and LOCALSTACK_HOST are supplied the adapter uses localstack
        """
        adapter = NetCDFToZarrAdapter(Message(mock_message_for('fake.nc')))
        self.assertEqual(adapter.s3.client_kwargs['endpoint_url'], 'http://fake-host:4572')

    @patch.dict(os.environ, MOCK_ENV)
    @patch.object(NetCDFToZarrAdapter, '_callback_post')
    @mock_s3
    def test_conversion_failure(self, _callback_post):
        """
        Tests that when file conversion fails, e.g. due to an incorrect file format, Harmony receives an
        error callback and the exception gets rethrown.
        """
        filename = tempfile.mkstemp()[1]
        exception = None
        try:
            message = mock_message_for(filename)
            main(['harmony_netcdf_to_zarr', '--harmony-action', 'invoke', '--harmony-input', message])
        except Exception as e:
            exception = e
        finally:
            os.remove(filename)

        callbacks = parse_callbacks(_callback_post)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0], { 'error': 'Could not convert granule to Zarr: G000-TEST' })

        self.assertIsNotNone(exception)
        self.assertIs('NetCDF: Unknown file format' in str(exception), True)