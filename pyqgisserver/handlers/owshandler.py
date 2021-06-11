#
# Copyright 2018-2019 3liz
# Author: David Marteau
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

""" Qgis server handler
"""
import logging
from time import time

from ..logger import log_rrequest
from ..zeromq.client import RequestTimeoutError, RequestGatewayError, AsyncClient
from ..monitor import Monitor

from .basehandler import BaseHandler

from typing import Optional, Awaitable, List

LOGGER = logging.getLogger('SRVLOG')


class OwsHandler(BaseHandler):

    """ Proxy to Qgis 0MQ worker
    """
    def initialize(self, root: str, client: AsyncClient, timeout: int, 
                   monitor: Optional[Monitor]=None, 
                   filters: Optional[List]=None, http_proxy: bool=False) -> None:

        super().initialize()

        self._root        = root
        self._rootpath    = root
        self._client      = client
        self._timeout     = timeout
        self._monitor     = monitor
        self._filters     = filters or []
        self._proxy       = http_proxy
        self._stats       = self.application.stats

    async def prepare(self) -> Awaitable[None]:
        # Handle filters
        super().prepare()
        self._rootpath = self._root
        for filt in self._filters:
            path = await filt.apply( self )
            if path:
                self._rootpath = f"{self._root}{path}"

    async def handle_request(self, method: str, path: str, data: Optional=None ) -> Awaitable[None]:
        reqtime = time()
        try:
            proxy_url = self.proxy_url(self._proxy, self._rootpath, path)

            delta = None
            project_path = self.get_argument('MAP',default=None)
            query        = self.encode_arguments()

            headers = {}

            if project_path:
                headers['X-Map-Location']=project_path 
            if proxy_url: 
                headers['X-Forwarded-Url']=proxy_url

            if self.has_body_arguments:
                # Do not let qgis server handle url encoded prameters
                method = 'GET'
                data   = None

            self._stats.num_requests +=1

            response = await self._client.fetch(query=query, method=method, 
                                                headers=headers, data=data,
                                                timeout=self._timeout)
            status = response.status
            hdrs   = response.headers
            delta  = time() - reqtime

            log_rrequest(path, status, method, query, delta, hdrs)
           
            # Send response
            for k,v in hdrs.items():
                self.set_header(k,v)

            # Send CORS Header
            self.set_access_control_headers()

            if status == 206:
                # Partial response
                self.set_status(200)
                self.write(response.data)
                await self.flush()
                async for chunk in self._client.fetch_more(response, timeout=self._timeout):
                    self.write(chunk)
                    await self.flush()
                delta = time() - reqtime
            elif status == 509:
                self.send_error(status, reason="Server busy, please retry later")
            else:
                self.set_status(status)
                self.write(response.data)

        except RequestTimeoutError:
            status = 504
            delta = time() - reqtime
            self.send_error(status, reason="Request timeout error")
        except RequestGatewayError:
            status = 502
            delta = time() - reqtime
            self.send_error(status, reason="Backend request error")

        if status >= 500:
            self._stats.num_errors +=1

        if self._monitor:
            self._monitor.emit( status, self.request.arguments,  delta, 
                                meta=self.request.headers)

    async def get(self, endpoint: str="") -> Awaitable[None]:
        """ Handle Get method
        """
        await self.handle_request('GET', endpoint)
          
    async def post(self, endpoint: str="") -> Awaitable[None]:
        """ Handle Post method
        """
        await self.handle_request('POST', endpoint, data=self.request.body)
        
    def options(self, endpoint: Optional[str]=None) -> None:
        """ Implement OPTION for validating CORS
        """
        self.set_option_headers('GET, POST, OPTIONS')



