# Tweepy
# Copyright 2009-2021 Joshua Roesslein
# See LICENSE for details.

import logging
import sys
import time

import requests
from urllib.parse import quote, urlencode

from tweepy.error import is_rate_limit_error_message, RateLimitError, TweepError
from tweepy.models import Model

log = logging.getLogger(__name__)


class APIMethod:

    def __init__(self, *args, **kwargs):
        self.api = api = kwargs.pop('api')
        self.session = requests.Session()

        self.parser = kwargs.pop('parser', api.parser)

        allowed_param = kwargs.pop('allowed_param', [])
        self.build_parameters(allowed_param, args, kwargs)

        # Monitoring rate limits
        self._remaining_calls = None
        self._reset_time = None

    def build_parameters(self, allowed_param, args, kwargs):
        self.session.params = {}
        for idx, arg in enumerate(args):
            if arg is None:
                continue
            try:
                self.session.params[allowed_param[idx]] = str(arg)
            except IndexError:
                raise TweepError('Too many parameters supplied!')

        for k, arg in kwargs.items():
            if arg is None:
                continue
            if k in self.session.params:
                raise TweepError(f'Multiple values for parameter {k} supplied!')

            self.session.params[k] = str(arg)

        log.debug("PARAMS: %r", self.session.params)

    def execute(self, method, path, *, headers=None, json_payload=None,
                payload_list=False, payload_type=None, post_data=None,
                require_auth=False, return_cursors=False, upload_api=False,
                use_cache=True):
        # If authentication is required and no credentials
        # are provided, throw an error.
        if require_auth and not self.api.auth:
            raise TweepError('Authentication required!')

        self.api.cached_result = False

        # Pick correct URL root to use
        if upload_api:
            api_root = self.api.upload_root
        else:
            api_root = self.api.api_root

        if upload_api:
            host = self.api.upload_host
        else:
            host = self.api.host

        # Build the request URL
        url = api_root + path
        full_url = 'https://' + host + url

        # Query the cache if one is available
        # and this request uses a GET method.
        if use_cache and self.api.cache and method == 'GET':
            cache_result = self.api.cache.get(f'{url}?{urlencode(self.session.params)}')
            # if cache result found and not expired, return it
            if cache_result:
                # must restore api reference
                if isinstance(cache_result, list):
                    for result in cache_result:
                        if isinstance(result, Model):
                            result._api = self.api
                else:
                    if isinstance(cache_result, Model):
                        cache_result._api = self.api
                self.api.cached_result = True
                return cache_result

        # Continue attempting request until successful
        # or maximum number of retries is reached.
        retries_performed = 0
        while retries_performed < self.api.retry_count + 1:
            if (self.api.wait_on_rate_limit and self._reset_time is not None
                and self._remaining_calls is not None
                and self._remaining_calls < 1):
                # Handle running out of API calls
                sleep_time = self._reset_time - int(time.time())
                if sleep_time > 0:
                    if self.api.wait_on_rate_limit_notify:
                        log.warning(f"Rate limit reached. Sleeping for: {sleep_time}")
                    time.sleep(sleep_time + 1)  # Sleep for extra sec

            # Apply authentication
            auth = None
            if self.api.auth:
                auth = self.api.auth.apply_auth()

            # Execute request
            try:
                resp = self.session.request(method,
                                            full_url,
                                            headers=headers,
                                            data=post_data,
                                            json=json_payload,
                                            timeout=self.api.timeout,
                                            auth=auth,
                                            proxies=self.api.proxy)
            except Exception as e:
                raise TweepError(f'Failed to send request: {e}').with_traceback(sys.exc_info()[2])

            if 200 <= resp.status_code < 300:
                break

            rem_calls = resp.headers.get('x-rate-limit-remaining')
            if rem_calls is not None:
                self._remaining_calls = int(rem_calls)
            elif self._remaining_calls is not None:
                self._remaining_calls -= 1

            reset_time = resp.headers.get('x-rate-limit-reset')
            if reset_time is not None:
                self._reset_time = int(reset_time)

            retry_delay = self.api.retry_delay
            if resp.status_code in (420, 429) and self.api.wait_on_rate_limit:
                if self._remaining_calls == 0:
                    # If ran out of calls before waiting switching retry last call
                    continue
                if 'retry-after' in resp.headers:
                    retry_delay = float(resp.headers['retry-after'])
            elif self.api.retry_errors and resp.status_code not in self.api.retry_errors:
                # Exit request loop if non-retry error code
                break

            # Sleep before retrying request again
            time.sleep(retry_delay)
            retries_performed += 1

        # If an error was returned, throw an exception
        self.api.last_response = resp
        if resp.status_code and not 200 <= resp.status_code < 300:
            try:
                error_msg, api_error_code = self.parser.parse_error(resp.text)
            except Exception:
                error_msg = f"Twitter error response: status code = {resp.status_code}"
                api_error_code = None

            if is_rate_limit_error_message(error_msg):
                raise RateLimitError(error_msg, resp)
            else:
                raise TweepError(error_msg, resp, api_code=api_error_code)

        # Parse the response payload
        return_cursors = (return_cursors or
                          'cursor' in self.session.params or 'next' in self.session.params)
        result = self.parser.parse(self, resp.text, payload_list=payload_list,
                                   payload_type=payload_type,
                                   return_cursors=return_cursors)

        # Store result into cache if one is available.
        if use_cache and self.api.cache and method == 'GET' and result:
            self.api.cache.store(f'{url}?{urlencode(self.session.params)}', result)

        return result


def bind_api(*args, **kwargs):
    http_method = kwargs.pop('method', 'GET')
    path = kwargs.pop('path')
    headers = kwargs.pop('headers', {})
    json_payload = kwargs.pop('json_payload', None)
    payload_list = kwargs.pop('payload_list', False)
    payload_type = kwargs.pop('payload_type', None)
    post_data = kwargs.pop('post_data', None)
    require_auth = kwargs.pop('require_auth', False)
    return_cursors = kwargs.pop('return_cursors', False)
    upload_api = kwargs.pop('upload_api', False)
    use_cache = kwargs.pop('use_cache', True)

    method = APIMethod(*args, **kwargs)
    try:
        if kwargs.get('create'):
            return method
        else:
            return method.execute(
                http_method, path, headers=headers, json_payload=json_payload,
                payload_list=payload_list, payload_type=payload_type,
                post_data=post_data, require_auth=require_auth,
                return_cursors=return_cursors, upload_api=upload_api,
                use_cache=use_cache
            )
    finally:
        method.session.close()


def pagination(mode):
    def decorator(method):
        method.pagination_mode = mode
        return method
    return decorator


def payload(payload_type, **payload_kwargs):
    payload_list = payload_kwargs.get('list', False)
    def decorator(method):
        def wrapper(*args, **kwargs):
            kwargs['payload_list'] = payload_list
            kwargs['payload_type'] = payload_type
            return method(*args, **kwargs)
        wrapper.payload_list = payload_list
        wrapper.payload_type = payload_type
        return wrapper
    return decorator
