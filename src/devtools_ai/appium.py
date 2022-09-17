import base64
import logging
import requests
import time
import urllib.parse
import uuid
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
        self.version = 'appium-0.1.25'
        self.root_elem = None
        caps = driver.desired_capabilities
        self.automation_name = caps.get('automationName', '')
        if caps.get('platformName', '') == 'Android' and self.automation_name == '':
            self.automation_name = 'UiAutomator2'
        self._driver_type = 'appium'
        SeleniumDriverCore.__init__(self, driver, api_key, initialization_dict)
        self._driver_type = 'appium'
        window_size = self.driver.get_window_size()
        screenshotBase64 = self._get_screenshot()
        im = Image.open(io.BytesIO(base64.b64decode(screenshotBase64)))
        width, height = im.size
        self.multiplier = max(1.0, 1.0 * width / window_size['width'])
        self.is_espresso = self.automation_name.lower() == 'espresso'
        self.is_ios = self.automation_name.lower() == 'xcuitest'


    def implicitly_wait(self, wait_time):
        self.driver.implicitly_wait(wait_time)

    def _get_screenshot(self):
        return self.driver.get_screenshot_as_base64()


    def _classify(self, element_name, is_backup=False):
        msg = ''
        if self.test_case_creation_mode:
            self._test_case_upload_screenshot(element_name)
            element_box, needs_reload = self._test_case_get_box(element_name)
            if element_box:
                real_elem = self._match_bounding_box_to_web_element(element_box, multiplier=self.multiplier)
                #element = ai_elem(real_elem.parent, real_elem, element_box, self.driver,
                #                  self.multiplier)
                element = real_elem
                return element, self.last_test_case_screenshot_uuid, msg
            else:
                event_id = str(uuid.uuid4())
                label_url = f'{self.url}/testcase/label?test_case_name={urllib.parse.quote(self.test_case_uuid)}&event_id={event_id}&api_key={self.api_key}'
                log.info('Waiting for bounding box of element {} to be drawn in the UI: \n\t{}'.format(element_name,
                                                                                                       label_url))
                webbrowser.open(label_url)
                while True:
                    element_box, needs_reload = self._test_case_get_box(element_name, event_id=event_id)
                    if element_box is not None:
                        print('Element was labeled, moving on')
                        real_elem = self._match_bounding_box_to_web_element(element_box, multiplier=self.multiplier)
                        element = ai_elem(real_elem.parent, real_elem._id, element_box, self.driver,
                                          self.multiplier, smart_driver=self, base_elem=real_elem)
                        return element, self.last_test_case_screenshot_uuid, msg

                    if needs_reload:
                        print('hot release')
                        self._test_case_upload_screenshot(element_name)
                    time.sleep(1)
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
                #element = ai_elem(real_elem.parent, real_elem, resp_data['box'], self.driver,
                #                  self.multiplier)
                element = real_elem
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
                for _ in range(2):
                    try:
                        r = requests.post(classify_url, json=data, verify=False, timeout=10)
                        break
                    except Exception:
                        log.error('error during detect')
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
                element = ai_elem(parent_elem, elem_id, element_box, self.driver, self.multiplier, smart_driver=self, base_elem=real_elem)
            except Exception:
                logging.exception('exception during classification')
            return element, run_key, msg

    def _match_bounding_box_to_web_element(self, bounding_box, multiplier=1):
        """
            We have to ba hacky about this becasue Selenium does not let us click by coordinates.
            We retrieve all elements, compute the IOU between the bounding_box and all the elements and pick the best match.
        """
        # Adapt box to local coordinates
        multiplier = max(1, multiplier)
        new_box = {'x': bounding_box['x'] / multiplier, 'y': bounding_box['y'] / multiplier,
                   'width': bounding_box['width'] / multiplier, 'height': bounding_box['height'] / multiplier}

        # Get all elements
        try:
            elements = self.driver.find_elements(by='xpath', value='//*')
        except StaleElementReferenceException:
            elements = self.driver.find_elements(by='xpath', value='//*')

        # Compute IOU
        iou_scores = []
        for element in elements:
            iou_scores.append(self._iou_boxes(new_box, element.rect))
        iou_composite = sorted(zip(iou_scores, elements), reverse=True, key=lambda x: x[0])
        # Pick the best match
        """
        We have to be smart about element selection here because of clicks being intercepted and what not, so we basically
        examine the elements in order of decreasing score, where score > 0. As long as the center of the box is within the elements,
        they are a valid candidate. If none of them is of type input, we pick the one with maxIOU, otherwise we pick the input type,
        which is 90% of test cases.
        """
        composite = filter(lambda x: x[0] > 0, iou_composite)
        composite = list(filter(lambda x: self._center_hit(new_box, x[1].rect), composite))

        attribute_for_class = "className"
        if self.is_espresso:
            attribute_for_class = "class"
        elif self.is_ios:
            attribute_for_class = "type"

        interactable_elements = ["input", "edittext", "edit", "select", "dropdown", "button", "textfield", "textarea", "picker", "spinner"]
        non_interactable_elements = ["layout"]

        for score, element in composite:
            for interactable_class in interactable_elements:
                if interactable_class in element.get_attribute(attribute_for_class).lower() and score > 0.6 * composite[0][0]:
                    contains_non_interactable = False
                    for non_interactable_class in non_interactable_elements:
                        if non_interactable_class in element.get_attribute(attribute_for_class).lower():
                            contains_non_interactable = True
                            break
                    if not contains_non_interactable:
                        return element

        if iou_composite[0][0] < 0.25:
            raise NoElementFoundException('Could not find any web element under the center of the bounding box')
        else:
            for score, element in composite:
                if element.tag_name == 'input' or element.tag_name == 'button':
                    return element
            return iou_composite[0][1]


class ai_elem(webdriver.webelement.WebElement):
    def __init__(self, parent, _id, elem, driver, multiplier=1.0, smart_driver=None, base_elem=None):
        super(ai_elem, self).__init__(parent, _id)
        multiplier = max(1, multiplier)
        self.driver = driver
        self.smart_driver = smart_driver
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
        self.real_elem = base_elem

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
        if not self.smart_driver.is_ios:
            actions = ActionChains(self.driver)
            actions.send_keys(value)
            actions.perform()
        else:
            self.real_elem.send_keys(value)

    def submit(self):
        self.send_keys('\n', click_first=False)


class NoElementFoundException(Exception):
    pass