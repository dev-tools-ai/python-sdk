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
import urllib.parse
import uuid
import warnings
import webbrowser

import io
from distutils.util import strtobool
from PIL import Image

from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import StaleElementReferenceException

from appium import webdriver

import requests.packages.urllib3

requests.packages.urllib3.disable_warnings()

log = logging.getLogger(__name__)

from .utils.selenium_core import SeleniumDriverCore

class SmartDriver(SeleniumDriverCore):
    def __init__(self, driver, api_key, initialization_dict={}):
        self.version = 'appium-0.1.19'
        self.root_elem = None
        SeleniumDriverCore.__init__(self, driver, api_key, initialization_dict)
        window_size = self.driver.get_window_size()
        screenshotBase64 = self._get_screenshot()
        im = Image.open(io.BytesIO(base64.b64decode(screenshotBase64)))
        width, height = im.size
        self.multiplier = 1.0 * width / window_size['width']


    def implicitly_wait(self, wait_time):
        self.driver.implicitly_wait(wait_time)

    def _get_screenshot(self):
        return self.driver.get_screenshot_as_base64()


    def _classify(self, element_name):
        msg = ''
        if self.test_case_creation_mode:
            self._test_case_upload_screenshot(element_name)
            element_box = self._test_case_get_box(element_name)
            if element_box:
                real_elem = self._match_bounding_box_to_web_element(element_box, multiplier=self.multiplier)
                element = ai_elem(real_elem.parent, real_elem, element_box, self.driver,
                                  self.multiplier)
                return element, self.last_test_case_screenshot_uuid, msg
            else:
                label_url = self.url + '/testcase/label?test_case_name=' + urllib.parse.quote(self.test_case_uuid)
                log.info('Waiting for bounding box of element {} to be drawn in the UI: \n\t{}'.format(element_name,
                                                                                                       label_url))
                webbrowser.open(label_url)
                while True:
                    element_box = self._test_case_get_box(element_name)
                    if element_box is not None:
                        print('Element was labeled, moving on')
                        real_elem = self._match_bounding_box_to_web_element(element_box, multiplier=self.multiplier)
                        element = ai_elem(real_elem.parent, real_elem, element_box, self.driver,
                                          self.multiplier)
                        return element, self.last_test_case_screenshot_uuid, msg
                    time.sleep(2)
        else:
            element = None
            run_key = None
            # Call service
            ## Get screenshot & page source
            screenshotBase64 = self._get_screenshot()
            key = self.get_screenshot_hash(screenshotBase64)
            resp_data = self._check_screenshot_exists(key, element_name)
            if resp_data['success'] and 'box' in resp_data:
                if self.debug:
                    print(f'Found cached box in action info for {element_name} using that')
                real_elem = self._match_bounding_box_to_web_element(resp_data['box'], multiplier=self.multiplier)
                element = ai_elem(real_elem.parent, real_elem, resp_data['box'], self.driver,
                                  self.multiplier)
                return element, key, msg

            source = ''
            # Check results
            try:
                data = {
                    'screenshot': screenshotBase64,
                    'source': source,
                    'api_key': self.api_key,
                    'label': element_name,
                }
                classify_url = self.url + '/detect'
                start = time.time()
                r = requests.post(classify_url, json=data, verify=False)
                end = time.time()
                if self.debug:
                    print(f'Classify time: {end - start}')
                response = r.json()
                if not response['success']:
                    classification_error_msg = response['message'].replace(self.default_prod_url, self.url)
                    raise Exception(classification_error_msg)
                run_key = response['screenshot_uuid']
                msg = response.get('message', '')
                msg = msg.replace(self.default_prod_url, self.url)

                element_box = response['predicted_element']
                if self.use_ai_elem:
                    parent_elem = None
                    real_elem = element_box
                    if self.root_elem is None:
                        self.root_elem =  self.driver.find_elements(by='xpath', value='//*')[0]._id

                    elem_id = self.root_elem
                else:
                    real_elem = self._match_bounding_box_to_web_element(
                        element_box, multiplier=self.multiplier)
                    parent_elem = real_elem.parent
                    elem_id = real_elem._id
                element = ai_elem(parent_elem, elem_id, element_box, self.driver, self.multiplier)
            except Exception:
                logging.exception('exception during classification')
            return element, run_key, msg

    def _match_bounding_box_to_web_element(self, bounding_box, multiplier=1):
        """
            We have to ba hacky about this becasue Selenium does not let us click by coordinates.
            We retrieve all elements, compute the IOU between the bounding_box and all the elements and pick the best match.
        """
        # Adapt box to local coordinates
        new_box = {'x': bounding_box['x'] / multiplier, 'y': bounding_box['y'] / multiplier,
                   'width': bounding_box['width'] / multiplier, 'height': bounding_box['height'] / multiplier}

        # Get all elements
        try:
            elements = self.driver.find_elements_by_xpath("//*")
        except StaleElementReferenceException:
            self.driver.refresh()
            elements = self.driver.find_elements_by_xpath("//*")

        # Compute IOU
        iou_scores = []
        for element in elements:
            iou_scores.append(self._iou_boxes(new_box, element.rect))
        composite = sorted(zip(iou_scores, elements), reverse=True, key=lambda x: x[0])
        # Pick the best match
        """
        We have to be smart about element selection here because of clicks being intercepted and what not, so we basically
        examine the elements in order of decreasing score, where score > 0. As long as the center of the box is within the elements,
        they are a valid candidate. If none of them is of type input, we pick the one with maxIOU, otherwise we pick the input type,
        which is 90% of test cases.
        """
        composite = filter(lambda x: x[0] > 0, composite)
        composite = list(filter(lambda x: self._center_hit(new_box, x[1].rect), composite))

        if len(composite) == 0:
            raise NoElementFoundException('Could not find any web element under the center of the bounding box')
        else:
            for score, element in composite:
                if element.tag_name == 'input' or element.tag_name == 'button':
                    return element
            return composite[0][1]
class ai_elem(webdriver.webelement.WebElement):
    def __init__(self, parent, _id, elem, driver, multiplier=1.0):
        super(ai_elem, self).__init__(parent, _id)
        self.driver = driver
        self._text = elem.get('text', '')
        self._size = {'width': elem.get('width', 0) / multiplier, 'height': elem.get('height', 0) / multiplier}
        self._location = {'x': elem.get('x', 0) / multiplier, 'y': elem.get('y', 0) / multiplier}
        self._property = elem.get('class', '')
        self._rect = {}
        self.rect.update(self._size)
        self.rect.update(self._location)
        self._tag_name = elem.get('class', '')
        self._cx = elem.get('x', 0) / multiplier + elem.get('width', 0) / multiplier / 2
        self._cy = elem.get('y', 0) / multiplier + elem.get('height', 0) / multiplier / 2

    @property
    def size(self):
        return self._size

    @property
    def location(self):
        return self._location

    @property
    def rect(self):
        return self._rect

    @property
    def tag_name(self):
        return self._tag_name

    def click(self):
        self.driver.tap([(self._cx, self._cy)])

    def send_keys(self, value, click_first=True):
        if click_first:
            self.click()
        actions = ActionChains(self.driver)
        actions.send_keys(value)
        actions.perform()

    def submit(self):
        self.send_keys('\n', click_first=False)


class NoElementFoundException(Exception):
    pass