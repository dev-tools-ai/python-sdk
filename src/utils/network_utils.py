import requests
import logging

log = logging.getLogger(__name__)

class NetworkUtils:
    def __init__(self, url):
        self.url = url

    def make_json_post_request(self, route, data, timeout_error_message, timeout_variable, generic_error_message=None,
                               tries=3):
        # Verify is False as the lets encrypt certificate raises issue on mac.
        res = {'success': False, 'message': 'did not run'}
        local_timeout = timeout_variable
        if generic_error_message is None:
            generic_error_message = 'Error making request to ' + route
        for _ in range(tries):
            try:
                log.debug('Making request to ' + route)
                url = self.url.rstrip('/') + route
                res = requests.post(url, json=data, verify=False, timeout=local_timeout).json()
                break
            except requests.exceptions.ConnectTimeout:
                local_timeout = local_timeout * 2
                log.debug(route)
                try:  # just in case the timeout message does not have a variable to format to too many variablas to format
                    log.error(timeout_error_message % local_timeout)
                    log.error(generic_error_message)
                except Exception:
                    log.error(timeout_error_message)
            except requests.exceptions.ConnectionError as e:
                if 'Connection aborted' in str(e) or 'timeout' in str(e):
                    local_timeout = local_timeout * 2
                    log.debug(route)
                    try:  # just in case the timeout message does not have a variable to format to too many variablas to format
                        log.error(timeout_error_message % local_timeout)
                        log.error(generic_error_message)
                    except Exception:
                        log.error(timeout_error_message)
                else:
                    raise e
            except requests.exceptions.ReadTimeout as e:
                local_timeout = local_timeout * 2
                log.debug(route)
                try:  # just in case the timeout message does not have a variable to format to too many variablas to format
                    log.error(timeout_error_message % local_timeout)
                except Exception:
                    log.error(timeout_error_message)
                    log.error(generic_error_message)
            except Exception as e:
                log.info(f'Exception type: {type(e)} and {e}')
                log.exception(e)
                log.error(generic_error_message)
        return res
