import base64
import hashlib
import json
import logging
import os
import platform
import requests
import sys
import time
import traceback
import uuid
import warnings

import io
from distutils.util import strtobool
from PIL import Image
from packaging import version
import selenium

if version.parse(selenium.__version__) < version.parse('4.0.0'):
    old_selenium = True
else:
    old_selenium = False

from selenium.webdriver.common.by import By

requests.packages.urllib3.disable_warnings()

log = logging.getLogger(__name__)

class SeleniumDriverCore(object):
    def __init__(self, driver, api_key=None, initialization_options={}):
        """
        Initialize DevTools SmartDriver.
        :Args:
         - driver: The already initialzed driver object.
         - api_key: Your API key to use for to test. If None will look for file ~/.smartdriver
         - initialization_options: Additional options that you can use to customize the smartdriver.

        :Returns:
         - Driver - the new driver object to use for your tests
        """
        self.driver = driver
        if api_key is None:
            if 'DEVTOOLSAI_API_KEY' in os.environ:
                api_key = os.environ['DEVTOOLSAI_API_KEY']
            elif os.path.exists(os.path.expanduser('~/.smartdriver')):
                with open(os.path.expanduser('~/.smartdriver')) as inFile:
                    api_key = json.loads(inFile.read()).get('api_key', None)

            # No API key, so return
            if api_key is None:
                return driver
        self.api_key = api_key
        self.last_test_case_screenshot_uuid = None
        self.run_id = str(uuid.uuid1())
        self.debug = initialization_options.get('debug', False)
        self.train = initialization_options.get('train', False)
        self.use_cdp = initialization_options.get('use_cdp', False)
        self.use_ai_elem = initialization_options.get('use_ai_elem', False)
        self.url = initialization_options.get('server_url',
                os.environ.get('DEVTOOLSAI_URL', 'https://smartdriver.dev-tools.ai'))
        self.default_prod_url = 'https://smartdriver.dev-tools.ai'
        test_case_name = initialization_options.get('test_case_name', None)
        if test_case_name is None:
            test_case_name = traceback.format_stack()[0].split()[1].split('/')[-1].split('.py')[0]
        self.test_case_uuid = test_case_name
        try:
            self.test_case_creation_mode = strtobool(os.environ.get('DEVTOOLSAI_INTERACTIVE', '0')) == 1
        except Exception:
            self.test_case_creation_mode = False
        if self.test_case_creation_mode:
            self.use_classifier_during_creation = initialization_options.get('use_classifier_during_creation', True)

        self._checkin()
        window_size = self.driver.get_window_size()
        screenshotBase64 = self._get_screenshot()

        im = Image.open(io.BytesIO(base64.b64decode(screenshotBase64)))
        width, height = im.size
        self.multiplier = 1.0 * width / window_size['width']
        self_attrs = dir(self)
        # Disable warnings
        requests.packages.urllib3.disable_warnings()
        warnings.filterwarnings("ignore", category=DeprecationWarning)

        for a_name in dir(driver):
            if a_name in self_attrs:
                continue
            try:
                def _call_driver(*args, name=a_name, **kwargs):
                    v = getattr(self.driver, name)
                    return v(*args, **kwargs)

                v = getattr(self.driver, a_name)
                if hasattr(v, '__call__'):
                    setattr(self, a_name, _call_driver)
                else:
                    setattr(self, a_name, v)
            except Exception as err:
                continue

    def get(self, url):
        self.driver.get(url)
        for a_name in dir(self.driver):
            try:
                v = getattr(self.driver, a_name)
                if hasattr(v, '__call__'):
                    continue
                elif '__' == a_name[0:2]:
                    # Skip these as they mess with internal properties
                    continue
                else:
                    setattr(self, a_name, v)
            except Exception as err:
                continue

    def implicitly_wait(self, wait_time):
        self.driver.implicitly_wait(wait_time)

    def find_element(self, by='id', value=None, element_name=None):
        """
        Find an element given a By strategy and locator.
        :Usage:
            ::
                element = driver.find_element(By.ID, 'foo')
        :rtype: WebElement
        """

        # Try to classify with selector
        #    If success, call update_elem ('train_if_necessary': true)
        #    If NOT successful, call _classify
        #        If succesful, return element
        #        If NOT succesful, raise element not found with link
        if element_name is None:
            element_name = 'element_name_by_locator_By_%s:_%s' % (str(by).replace('.', '_').replace(' ', '_'), str(value).replace('.', '_').replace(' ', '_'))
        return self._generic_find_method(
            self.driver.find_element, element_name, by, value)

    def find_element_by_accessibility_id(self, accessibility_id, element_name=None):
        """
        Finds an element by an accessibility id.

        :Args:
         - accessibility_id: The name of the element to find.
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::
                element = driver.find_element_by_accessibility_id('foo')
        """
        return self.find_element(self, by=By.ACCESSIBILITY_ID, value=accessibility_id, element_name=element_name)

    def find_element_by_class_name(self, name, element_name=None):
        """
        Finds an element by class name.

        :Args:
         - name: The class name of the element to find.
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::
                element = driver.find_element_by_class_name('foo')
        """
        return self.find_element(by=By.CLASS_NAME, value=name, element_name=element_name)

    def find_element_by_css_selector(self, css_selector, element_name=None):
        """
        Finds an element by css selector.

        :Args:
         - css_selector - CSS selector string, ex: 'a.nav#home'
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_css_selector('#foo')
        """
        return self.find_element(by=By.CSS_SELECTOR, value=css_selector, element_name=element_name)

    def find_element_by_id(self, id_, element_name=None):
        """
        Finds an element by id.

        :Args:
         - id\\_ - The id of the element to be found.
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_id('foo')
        """
        return self.find_element(by=By.ID, value=id_, element_name=element_name)

    def find_element_by_link_text(self, link_text, element_name=None):
        """
        Finds an element by link text.

        :Args:
         - link_text: The text of the element to be found.
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_link_text('Sign In')
        """
        return self.find_element(by=By.LINK_TEXT, value=link_text, element_name=element_name)

    def find_element_by_name(self, name, element_name=None):
        """
        Finds an element by name.

        :Args:
         - name: The name of the element to find.
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_name('foo')
        """
        return self.find_element(by=By.NAME, value=name, element_name=element_name)

    def find_element_by_partial_link_text(self, link_text, element_name=None):
        """
        Finds an element by a partial match of its link text.

        :Args:
         - link_text: The text of the element to partially match on.
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_partial_link_text('Sign')
        """
        return self.find_element(by=By.PARTIAL_LINK_TEXT, value=link_text, element_name=element_name)

    def find_element_by_tag_name(self, name, element_name=None):
        """
        Finds an element by tag name.

        :Args:
         - name - name of html tag (eg: h1, a, span)
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_tag_name('h1')
        """
        return self.find_element(by=By.TAG_NAME, value=name, element_name=element_name)

    def find_element_by_xpath(self, xpath, element_name=None):
        """
        Finds an element by xpath.

        :Args:
         - xpath - The xpath locator of the element to find.
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_element_by_xpath('//div/td[1]')
        """
        return self.find_element(by=By.XPATH, value=xpath, element_name=element_name)

    def find_by_ai(self, element_name):
        """
        Finds an element using the ai.

        :Args:
         - element_name: The label name of the element to be classified.

        :Returns:
         - WebElement - the element if it was found

        :Raises:
         - NoSuchElementException - if the element wasn't found

        :Usage:
            ::

                element = driver.find_by_element_name('some_label')
        """
        el, key, msg = self._classify(element_name)

        if el is None:
            print(msg)
            raise Exception(msg)
        return el

    def _generic_find_method(self, find_method, element_name, *args):
        key = None
        msg = 'ai driver exception'

        # Run the standard selector
        try:
            driver_element = find_method(*args)
            if driver_element:
                try:
                    key = self._upload_screenshot_if_necessary(element_name)
                    if key is not None:
                        # Key is None when element is frozen or another issue during screenshot and upload.
                        self._update_elem(driver_element, key, element_name)
                except Exception:
                    log.error('error uploading screenshot to devtools. Continuing.')
            return driver_element
        except NoElementFoundException as e:
            log.exception(e)
        except Exception as err:
            # If this happens, then error during the driver call
            classified_element, key, msg = self._classify(element_name)
            if classified_element:
                log.error('Selector failed, using ai classifier element')
                return classified_element
            else:
                raise Exception(msg)
        return None

    def _checkin(self):
        """
        Check in the current session.
        """
        data = {'api_key': self.api_key,
                'os': platform.platform(),
                'sdk_version': self.version,
                'language': 'python3-' + sys.version,
                'test_case_name': self.test_case_uuid}
        try:
            res = requests.post(self.url + '/ping', json=data, timeout=3, verify=False)
            res = res.json()
            if not res['success']:
                log.error(res['message'])
        except Exception:
            pass

    def _update_elem(self, elem, screenshot_uuid, element_name, train_if_necessary=True):
        data = {
            'screenshot_uuid': screenshot_uuid,
            'retrain': train_if_necessary,
            'api_key': self.api_key,
            'label': element_name,
            'x': elem.rect['x'] * self.multiplier,
            'y': elem.rect['y'] * self.multiplier,
            'width': elem.rect['width'] * self.multiplier,
            'height': elem.rect['height'] * self.multiplier,
            'multiplier': self.multiplier,
            'test_case_name': self.test_case_uuid
        }
        try:
            action_url = self.url + '/add_action_info'
            # Verify is False as the lets encrypt certificate raises issue on mac.
            res = requests.post(action_url, json=data, verify=False)
            res = res.json()
            if not res['success']:
                log.error(res['message'])
        except Exception:
            pass

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

    def _check_screenshot_exists(self, screenshot_uuid, element_name):
        data = {'api_key': self.api_key, 'screenshot_uuid': screenshot_uuid, 'label': element_name}
        check_screenshot_url = self.url + '/exists_screenshot'
        start = time.time()
        r = requests.post(check_screenshot_url, json=data, verify=False).json()
        end = time.time()
        if self.debug:
            print(f'Cached bounding box request time: {end - start}')

        if not r['success']:
            log.error(r['message'])
        return r

    def _check_frozen(self, element_name):
        url = self.url + '/check_frozen'
        request_data = {'api_key': self.api_key,
                        'label': element_name
                        }
        resp = requests.post(url, json=request_data).json()
        return resp.get('is_frozen', True)


    def _upload_screenshot_if_necessary(self, element_name):
        if self._check_frozen(element_name):
            return None

        screenshotBase64 = self._get_screenshot()
        screenshot_uuid = self.get_screenshot_hash(screenshotBase64)
        # Check results
        try:
            response = self._check_screenshot_exists(screenshot_uuid, element_name)
            if self.debug:
                print(response)
            if response.get('exists_screenshot', False) or response.get('is_frozen', False):
                if response['is_frozen']:
                    if self.debug:
                        print(f'{element_name} is frozen, skipping upload')
                else:
                    if self.debug:
                        print(f'Screenshot {screenshot_uuid} already exists on remote')
                return screenshot_uuid
            else:
                if self.debug:
                    print(f'Screenshot {screenshot_uuid} does not exist on remote, uploading it')
                data = {'api_key': self.api_key, 'screenshot_uuid': screenshot_uuid,
                        'screenshot': screenshotBase64,
                        'label': element_name,
                        'test_case_name': self.test_case_uuid}
                upload_screenshot_url = self.url + '/upload_screenshot'
                start = time.time()
                r = requests.post(upload_screenshot_url, json=data, verify=False).json()
                end = time.time()
                if r['success'] == False:
                    log.error(f'Error uploading screenshot {screenshot_uuid}')
                    log.error(r['message'])
                else:
                    screenshot_uuid = r['screenshot_uuid']
                if self.debug:
                    print(f'Upload screenshot request time: {end - start}')
                return screenshot_uuid
        except Exception:
            log.exception('Error checking cached screenshot / uploading it from remote')

    def _test_case_get_box(self, label):
        """
            Checks for a bounding box given the last screenshot uuid that we got when uploading it.
        """
        box = None
        data = {'api_key': self.api_key, 'label': label, 'screenshot_uuid': self.last_test_case_screenshot_uuid,
                'run_classifier': self.use_classifier_during_creation}

        r = requests.post(self.url + '/testcase/get_action_info', json=data, verify=False).json()
        if r['success']:
            box = r['predicted_element']
        else:
            log.error(r['message'])
        return box


    def _test_case_upload_screenshot(self, label):
        """
            Uploads the screenshot to the server for test creation and retrieves the uuid / hash / key in return.
        """
        url = self.url + '/upload_screenshot'
        screenshotBase64 = self._get_screenshot()
        self.last_screenshot = screenshotBase64
        data = {'api_key': self.api_key,
                'test_case_name': self.test_case_uuid,
                'screenshot': screenshotBase64,
                'label': label,
                'is_interactive': True}
        res = requests.post(url, json=data, verify=False).json()
        if res['success']:
            self.last_test_case_screenshot_uuid = res['screenshot_uuid']
            self.last_screenshot = screenshotBase64
        else:
            raise Exception(res['message'])

    def _iou_boxes(self, box1, box2):
        return self._iou(box1['x'], box1['y'], box1['width'], box1['height'], box2['x'], box2['y'], box2['width'],
                         box2['height'])

    def _iou(self, x, y, w, h, xx, yy, ww, hh):
        return self._area_overlap(x, y, w, h, xx, yy, ww, hh) / (
                self._area(w, h) + self._area(ww, hh) - self._area_overlap(x, y, w, h, xx, yy, ww, hh))

    def _area_overlap(self, x, y, w, h, xx, yy, ww, hh):
        dx = min(x + w, xx + ww) - max(x, xx)
        dy = min(y + h, yy + hh) - max(y, yy)
        if (dx >= 0) and (dy >= 0):
            return dx * dy
        else:
            return 0

    def _area(self, w, h):
        return w * h

    def _center_hit(self, box1, box2):
        box1_center = box1['x'] + box1['width'] / 2, box1['y'] + box1['height'] / 2
        if box1_center[0] > box2['x'] and box1_center[0] < box2['x'] + box2['width'] and box1_center[1] > box2['y'] and \
                box1_center[1] < box2['y'] + box2['height']:
            return True
        else:
            return False

class NoElementFoundException(Exception):
    pass