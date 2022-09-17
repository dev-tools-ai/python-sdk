class LinkManager:
    def __init__(self):
        self.links_visited = []
        self.links_to_visit = []
        self.referrers = []
        self.depths = []

    def add_link(self, referrer, link, depth=0):
        if not self.seen(link) and not link in self.links_to_visit:
            self.links_to_visit.append(link)
            self.referrers.append(referrer)
            self.depths.append(depth)

    def get_link(self):
        return self.referrers.pop(0), self.links_to_visit.pop(0), self.depths.pop(0)

    def visited_link(self, link):
        self.links_visited.append(link)

    def visited_count(self):
        return len(self.links_visited)

    def has_more_links(self):
        return len(self.links_to_visit) > 0

    def seen(self, link):
        return link in self.links_visited

def is_js_error(message):
    if 'error' in message.lower():
        return True
    else:
        return False

from time import sleep


import json
import logging
import requests
import uuid

from urllib.parse import urlparse
import hashlib

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

class DataManager:
    def __init__(self, prod_url, api_key):
        self.prod_url = prod_url
        self.api_key = api_key

    def save_console_error(self, crawl_iteration, context_url, error_type, error_details, error_message):
        data = {
            'api_key': self.api_key,
            'crawl_iteration': crawl_iteration,
            'context_url': context_url,
            'error_type': error_type,
            'error_details': error_details,
            'error_message': error_message,
        }
        r = requests.post(self.prod_url + '/scanner/save_console_error', json=data)

    def save_network_error(self, crawl_iteration,
                                                context_url,
                                                referrer_url,
                                                request_url,
                                                status_code,
                                                error_type,
                                                error_details,
                                                screenshot_uuid,
                           element_location={'x': 0, 'y': 0, 'width': 0, 'height': 0}):
        if screenshot_uuid is None:
            screenshot_uuid = ''
        data = {
            'api_key': self.api_key,
            'crawl_iteration': crawl_iteration,
            'context_url': context_url,
            'referrer_url': referrer_url,
            'request_url': request_url,
            'status_code': status_code,
            'error_type': error_type,
            'error_details': error_details,
            'screenshot_uuid': screenshot_uuid,
            'element_location': element_location
        }
        r = requests.post(self.prod_url + '/scanner/save_network_error', json=data)

    def get_screenshot_hash(self, b64_screenshot):
        """
        Get the screenshot uuid from the base64 encoded screenshot
        """
        try:
            return hashlib.md5(b64_screenshot).hexdigest()
        except Exception as e:
            try:
                return hashlib.md5(b64_screenshot.encode('utf-8')).hexdigest()
            except Exception as e:
                try:
                    return hashlib.md5(b64_screenshot.decode('utf-8')).hexdigest()
                except Exception as e:
                    return None

    def upload_screenshot(self, crawl_iteration, screenshotBase64):
        screenshot_uuid = self.get_screenshot_hash(screenshotBase64)

        data = {'api_key': self.api_key,
                'screenshot_uuid': screenshot_uuid,
                'screenshot': screenshotBase64,
                'crawl_iteration': crawl_iteration}
        upload_screenshot_url = self.prod_url + '/scanner/upload_screenshot'
        try:
            r = requests.post(upload_screenshot_url, json=data, verify=False)
            if r.status_code == 200:
                uuid = r.json()['screenshot_uuid']
                return uuid
        except Exception as e:
            pass
        return None

class Scanner:
    def __init__(self, smart_driver, driver, prod_url, api_key):
        self.smart_driver = smart_driver
        self.driver = driver
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Console.enable", {})
        self.link_manager = LinkManager()
        self.data_manager = DataManager(prod_url, api_key)
        self.crawl_iteration = str(uuid.uuid4())

    def crawl_domain(self, url, max_depth=5):
        self.domain = urlparse(url).netloc
        self.link_manager.add_link(url, url, depth=0)
        # While there are links to visit
        while self.link_manager.has_more_links():
            referrer, link, depth = self.link_manager.get_link()
            try:
                if depth <= max_depth:
                    self.process_link(link, referrer, depth)
                else:
                    log.info(f'Skipping link {link} because it is too deep {depth}')
            except Exception as e:
                log.error(f"Error processing link {link}: {e}")

    def process_console_logs(self, console_logs, link):
        for l in console_logs:
            if (l['level'] == 'SEVERE'):
                log.debug(f"Bad JS: {l['message']}")
                self.data_manager.save_console_error(crawl_iteration=self.crawl_iteration,
                                                context_url=link,
                                                error_type='JS',
                                                error_details=l,
                                                error_message=l['message'])
            else:
                if is_js_error(l['message']):
                    log.debug(f"Bad JS: {l['message']}")
                    self.data_manager.save_console_error(crawl_iteration=self.crawl_iteration,
                                                    context_url=link,
                                                    error_type='JS',
                                                    error_details=l,
                                                    error_message=l['message'])

    def process_perf_logs(self, perf_logs, link, referrer):
        perf_logs = [json.loads(lr["message"])["message"] for lr in perf_logs]
        responses = [l for l in perf_logs if l["method"] == "Network.responseReceived"]
        for r in responses:
            status = r['params']['response']['status']
            if status >= 400:
                log.debug(f"Bad request: {status} {r['params']['response']['url']}")
                save_id = None
                box = {'x': 0, 'y': 0, 'width': 0, 'height': 0}
                used_referrer = False
                try:
                    try:
                        self.driver.get(referrer)
                        sleep(2)
                        partial_url = r['params']['response']['url'].lstrip('http://').lstrip('https://').lstrip(self.domain)
                        inpage_link = self.driver.find_element(By.XPATH, f"//a[contains(@href,'{partial_url}')]")
                        used_referrer = True
                    except NoSuchElementException as e:
                        self.driver.get(link)
                        sleep(2)
                        partial_url = r['params']['response']['url'].lstrip('http://').lstrip('https://').lstrip(self.domain)
                        inpage_link = self.driver.find_element(By.XPATH, f"//img[contains(@src,'{partial_url}')]")

                    screenshot = self.smart_driver._get_screenshot()
                    # save screenshot to file
                    box = {'x': inpage_link.location['x'], 'y': inpage_link.location['y'],
                           'width': inpage_link.size['width'], 'height': inpage_link.size['height']}
                    for k in box:
                        box[k] *= self.smart_driver.multiplier
                    save_id = self.data_manager.upload_screenshot(self.crawl_iteration, screenshot)
                except Exception as e:
                    pass
                if used_referrer:
                    context_url = referrer
                else:
                    context_url = link
                self.data_manager.save_network_error(crawl_iteration=self.crawl_iteration,
                                                context_url=context_url,
                                                referrer_url=referrer,
                                                request_url=r['params']['response']['url'],
                                                status_code=status,
                                                error_type='request',
                                                error_details=r,
                                                screenshot_uuid=save_id,
                                                element_location=box)

    def process_link(self, link, referrer, depth):
        _ = self.driver.get_log('browser')  # clear logs
        _ = self.driver.get_log("performance")
        self.driver.get(link)
        sleep(2.0)
        log.info(f"Processing link {link}")
        console_logs = self.driver.get_log("browser")
        self.process_console_logs(console_logs, link)

        perf_logs = self.driver.get_log("performance")
        self.process_perf_logs(perf_logs, link, referrer)

        log.info(f'Visited {link}')
        self.link_manager.visited_link(link)
        local_referrer = link

        for i in range(3):
            try:
                if urlparse(link).netloc == self.domain:
                    links = self.driver.find_elements(By.TAG_NAME, 'a')
                    for link in links:
                        if urlparse(link.get_attribute('href')).netloc == self.domain:
                            self.link_manager.add_link(local_referrer, link.get_attribute('href'), depth + 1)

                    imgs = self.driver.find_elements(By.TAG_NAME, 'img')
                    for img in imgs:
                        if urlparse(img.get_attribute('src')).netloc == self.domain:
                            self.link_manager.add_link(local_referrer, img.get_attribute('src'), depth + 1)
                    break
            except Exception as e:
                log.error(f"Error processing link {link}: {e}")
                sleep(2.0)
                continue