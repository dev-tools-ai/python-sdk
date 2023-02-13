import base64
import json
import logging
import requests
import time
import uuid
import urllib.parse
import warnings
import webbrowser


import io
from distutils.util import strtobool
from PIL import Image
from packaging import version
from time import sleep


import selenium

if version.parse(selenium.__version__) < version.parse('4.0.0'):
    old_selenium = True
else:
    old_selenium = False

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.common.exceptions import StaleElementReferenceException

from . import __version__ as base_version
from .utils.selenium_core import SeleniumDriverCore
from .utils.local_classify import LocalClassifier


requests.packages.urllib3.disable_warnings()

log = logging.getLogger(__name__)

class SmartDriver(SeleniumDriverCore):
    def __init__(self, driver, api_key=None, initialization_dict={}):
        self.version = 'selenium-' + base_version
        self.automation_name = driver.capabilities.get('browserName', '')
        SeleniumDriverCore.__init__(self, driver, api_key, initialization_dict)
        window_size = self.driver.get_window_size()
        self.window_size = window_size
        screenshotBase64 = self._get_screenshot()
        self._driver_type = 'selenium'
        im = Image.open(io.BytesIO(base64.b64decode(screenshotBase64)))
        width, height = im.size
        self.im_size = im.size
        self.multiplier = 1.0 * width / window_size['width']
        self_attrs = dir(self)
        # Disable warnings
        requests.packages.urllib3.disable_warnings()
        self.local_classify = None
        if self.do_local_caching:
            self.local_classify = LocalClassifier(self.url, self.api_key, template_match_threshold=self.local_match_threshold)
            for element_name in self.element_names_in_tc:
                self.local_classify.cache_templates_for_element(element_name)

        warnings.filterwarnings("ignore", category=DeprecationWarning)

    def _get_elem(self, screenshotBase64, element_name, offset):
        for i in range(1):
            try:
                data = {
                    "b64": screenshotBase64,
                    "use_gclf":  True
                }
                r = requests.post(url='http://kirfuzz.dev-tools.ai:5002/classify', json=data, timeout=100)
                raw_content = r.content
                elems = json.loads(raw_content)
                for e in elems:
                    if e.get('label') == element_name:
                        element = self._get_matched_elem(e)
                        return element
                return None
            except Exception as err:
                print(err)
                print('error getting entities:', err)
            time.sleep(1)
        return None

    def find_by_generic(self, element_name):
        """
        Find an element given a generic element name.
        :Usage:
            ::
                element = driver.find_by_generic('cart')
        :rtype: WebElement
        """
        page_offset = self.driver.execute_script('return window.pageYOffset')
        current_offset = page_offset
        window_height = self.driver.execute_script('return window.innerHeight')
        scroll_amount = int(0.8 * window_height)
        scroll_count = 0
        while scroll_count < 10:
            screenshotBase64 = self._get_screenshot()
            screenshot_uuid = self.get_screenshot_hash(screenshotBase64)
            # Check if element is on the page
            elem = self._get_elem(screenshotBase64, element_name, current_offset)
            if elem is not None:
                return elem
            # Scroll
            page_offset = self.driver.execute_script('return window.pageYOffset')
            self._scroll_page(scroll_amount)
            current_offset = self.driver.execute_script('return window.pageYOffset')
            if current_offset == page_offset:
                # End of page
                break

        raise NoElementFoundException("No element located on the screen")


    def _get_screenshot(self):
        if self.use_cdp:
            screenshotBase64 = self.driver.execute_cdp_cmd('Page.captureScreenshot', {})['data']
        else:
            screenshotBase64 = self.driver.get_screenshot_as_base64()
        return screenshotBase64

    def _get_matched_elem(self, element_box):
        if self.use_cdp:
            parent_elem = None
            real_elem = element_box
        else:
            try:
                real_elem = self._match_bounding_box_to_selenium_element(element_box, multiplier=self.multiplier)
                parent_elem = real_elem.parent
            except NoElementFoundException as e:
                log.warning(
                    "Could not find a regular Selenium element at coordinates, switching to AIElement, some attributes might be missing.")
                parent_elem = None
                real_elem = element_box
        element = ai_elem(parent_elem, real_elem, element_box, self.driver, self.multiplier, self.use_cdp)
        return element


    def process_exist_screenshot_response(self, element_name, key, msg, tresp_data, is_backup=False):
        if self.debug:
            print(f'Found cached box in action info for {element_name} using that')
        element_box = tresp_data['predicted_element']
        expected_offset = tresp_data['page_offset']
        element_box['y'] += expected_offset
        element = self._get_matched_elem(element_box)
        try:
            real_xpath = self._generate_xpath(element)
            self._send_warning(real_xpath, element_name, key, tresp_data, is_backup=is_backup)
        except Exception as e:
            log.debug(e)
        return element, key, msg

    def _classify(self, element_name, is_backup=True):
        msg = ''
        if self.test_case_creation_mode:
            if self.do_local_caching:
                screenshotBase64 = self._get_screenshot()
                self.last_test_case_screenshot_uuid = self.get_screenshot_hash(screenshotBase64)

                element_box = self.local_classify.classify_element(element_name, screenshotBase64)
                if element_box is not None:
                    msg = 'Found using local cache'
                    element = self._get_matched_elem(element_box)
                    return element, self.last_test_case_screenshot_uuid, msg
                else:
                    msg = 'Not found using local cache'
                needs_reload = False
                has_training_data = True


            self._test_case_upload_screenshot(element_name)
            element_box, needs_reload, has_training_data = self._test_case_get_box(element_name)
            if element_box:
                element = self._get_matched_elem(element_box)
                return element, self.last_test_case_screenshot_uuid, msg
            else:
                if has_training_data:
                    response = self._classify_full_screen(element_name)
                    if response is not None and response['success']:
                        element_box = response['predicted_element']
                        self.page_offset = self.driver.execute_script('return window.pageYOffset')
                        element_box['y'] += self.page_offset * self.multiplier
                        element = self._get_matched_elem(element_box)
                        return element, self.last_test_case_screenshot_uuid, msg
                    else:
                        # scroll back up
                        self._scroll_page(-100000)
                event_id = str(uuid.uuid4())
                label_url = f'{self.url}/testcase/label?test_case_name={urllib.parse.quote(self.test_case_uuid)}&event_id={event_id}&api_key={self.api_key}'
                log.info('Waiting for bounding box of element {} to be drawn in the UI: \n\t{}'.format(element_name,
                                                                                                       label_url))
                webbrowser.open(label_url)
                while True:
                    element_box, needs_reload, _ = self._test_case_get_box(element_name, event_id=event_id)
                    if element_box is not None:
                        element = self._get_matched_elem(element_box)
                        return element, self.last_test_case_screenshot_uuid, msg

                    if needs_reload:
                        self._test_case_upload_screenshot(element_name)

                    time.sleep(2)
        else:
            element = None
            run_key = None
            # Call service
            ## Get screenshot & page source
            screenshotBase64 = self._get_screenshot()
            key = self.get_screenshot_hash(screenshotBase64)

            if self.do_local_caching:
                local_pred = self.local_classify.classify_element(element_name, screenshotBase64)
                if local_pred is not None:
                    msg = 'Found using local cache'
                    element = self._get_matched_elem(local_pred)
                    return element, key, msg

            resp_data = self._check_screenshot_exists(key, element_name)

            if resp_data['success'] and 'predicted_element' in resp_data and resp_data['predicted_element'] is not None:
                log.debug(resp_data.get("message", ""))
                current_offset = self.driver.execute_script('return window.pageYOffset;')
                bottom_page_offset = current_offset + self.window_size['height']

                real_offset = resp_data['page_offset'] / self.multiplier
                if real_offset > bottom_page_offset or real_offset < current_offset:
                    scroll_offset = int(real_offset - current_offset)
                    self._scroll_page(scroll_offset)

                    sleep(1)
                    screenshotBase64 = self._get_screenshot()
                    key = self.get_screenshot_hash(screenshotBase64)
                    resp_data = self._check_screenshot_exists(key, element_name)
                if resp_data['success'] and 'predicted_element' in resp_data and resp_data[
                    'predicted_element'] is not None:
                    return self.process_exist_screenshot_response(element_name, key, msg, resp_data, is_backup=is_backup)

            source = ''

            # Check results
            try:
                data = {'screenshot': screenshotBase64,
                        'source': source,
                        'api_key': self.api_key,
                        'label': element_name,
                        'test_case_name': self.test_case_uuid,
                        'do_exact_match_first': self.do_exact_match_first,
                        'exact_match_first_threshold': self.exact_match_first_threshold,
                        }
                start = time.time()
                response = self.make_json_post_request('/detect', data, self.detect_timeout_message, self.detect_timeout)
                end = time.time()
                if self.debug:
                    print(f'Classify time: {end - start}')
                if not response['success']:
                    # Could be a failure or an element off screen
                    response = self._classify_full_screen(element_name)
                    if not response['success']:
                        classification_error_msg = response['message'].replace(self.default_prod_url, self.url)
                        log.debug(classification_error_msg)
                        raise Exception(classification_error_msg)
                run_key = response['screenshot_uuid']
                msg = response.get('message', '')
                msg = msg.replace(self.default_prod_url, self.url)
                log.debug(msg)

                element_box = response['predicted_element']
                self.page_offset = self.driver.execute_script('return window.pageYOffset')
                element_box['y'] += self.page_offset * self.multiplier
                element = self._get_matched_elem(element_box)
                if element._is_real_elem:
                    try:
                        real_xpath = self._generate_xpath(element)
                        self._send_warning(real_xpath, element_name, key, response, is_backup=is_backup)
                    except Exception as e:
                        log.debug(e)
            except Exception as e:
                log.error(e)
                logging.exception('exception during classification')
            return element, run_key, msg

    def _classify_full_screen(self, element_name):
        self._scroll_page(-100000) # go to the top
        last_offset = -1
        offset = 1
        responses = []
        window_height = self.window_size['height']
        r = None
        while offset > last_offset:
            last_offset = offset
            screenshotBase64 = self._get_screenshot()
            data = {'screenshot': screenshotBase64,
                    'source': '',
                    'api_key': self.api_key,
                    'label': element_name,
                    'test_case_name': self.test_case_uuid,
                    'do_exact_match_first': self.do_exact_match_first,
                    'exact_match_first_threshold': self.exact_match_first_threshold,}
            r = self.make_json_post_request('/detect', data, self.detect_timeout_message, self.detect_timeout)
            if r['success']:
                return r
            self._scroll_page(window_height)
            sleep(0.2)
            offset = self.driver.execute_script('return window.pageYOffset')
        return r

    def _pick_best_fs_response(self, responses):
        """
        First we check if we have zero success, if yes we pick the first one,
        one unique success, if yes we pick that
        more than that, we pick the one with highest score.
        """

        success_ct = len([r for r in responses if r['success']])
        if success_ct == 0:
            return responses[0]
        elif success_ct == 1:
            for r in responses:
                if r['success']:
                    return r
        else:
            best_score = 0
            best_response = None
            for r in responses:
                if r['success']:
                    if r['score'] > best_score:
                        best_score = r['score']
                        best_response = r
            return best_response

    def _match_bounding_box_to_selenium_element_js(self, bounding_box, multiplier=1):
        multiplier = max(multiplier, 1)
        new_box = {'x': bounding_box['x'] / multiplier, 'y': bounding_box['y'] / multiplier, 'width': bounding_box['width'] / multiplier, 'height': bounding_box['height'] / multiplier}

        middle = (new_box['x'] + new_box['width'] / 2, new_box['y'] + new_box['height'] / 2)
        # get the element using "return document.elementFromPoint(arguments[0], arguments[1])"

        element = self.driver.execute_script("return document.elementFromPoint(arguments[0], arguments[1])", middle[0], middle[1])
        return element


    def _match_bounding_box_to_selenium_element(self, bounding_box, multiplier=1, offset=0):
        """
            We have to ba hacky about this becasue Selenium does not let us click by coordinates.
            We retrieve all elements, compute the IOU between the bounding_box and all the elements and pick the best match.
        """
        if self.use_fast_js_chopper:
            return self._match_bounding_box_to_selenium_element_js(bounding_box, multiplier=multiplier)

        # Adapt box to local coordinates
        multiplier = max(1, multiplier) # Make sure multiplier isn't 0.
        new_box = {'x': bounding_box['x'] / multiplier, 'y': bounding_box['y'] / multiplier,
                   'width': bounding_box['width'] / multiplier, 'height': bounding_box['height'] / multiplier}
        new_box['y'] += offset
        element_types = ["a", "input", "button", "img",  "*"]
        for element_type in element_types:
            # Get all elements
            try:
                elements = self.driver.find_elements(By.XPATH, "//" + element_type)
            except StaleElementReferenceException:
                elements = self.driver.find_elements(By.XPATH, "//" + element_type)

            # Compute IOU
            iou_scores = []
            for element in elements:
                try:
                    iou_scores.append(self._iou_boxes(new_box, element.rect))
                except StaleElementReferenceException:
                    iou_scores.append(0)
            composite = sorted(zip(iou_scores, elements), reverse=True, key=lambda x: x[0])
            # Pick the best match
            """
            We have to be smart about element selection here because of clicks being intercepted and what not, so we basically
            examine the elements in order of decreasing score, where score > 0. As long as the center of the box is within the elements,
            they are a valid candidate. If none of them is of type input, we pick the one with maxIOU, otherwise we pick the input type,
            which is 90% of test cases.
            """
            composite = filter(lambda x: x[0] > 0.2, composite)
            composite = list(filter(lambda x: self._center_hit(new_box, x[1].rect), composite))
            end = time.time()

            if len(composite) == 0:
                # No elements found matching, continue to next search
                continue
            else:
                for score, element in composite:
                    if score > composite[0][0] * 0.9:
                        return element
                return composite[0][1]
        raise NoElementFoundException('Could not find any web element under the center of the bounding box')


    def _iou_boxes(self, box1, box2):
        return self._iou(box1['x'], box1['y'], box1['width'], box1['height'], box2['x'], box2['y'], box2['width'], box2['height'])

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
        if box1_center[0] > box2['x'] and box1_center[0] < box2['x'] + box2['width'] and box1_center[1] > box2['y'] and box1_center[1] < box2['y'] + box2['height']:
            return True
        else:
            return False

class ai_elem(webdriver.remote.webelement.WebElement):
    def __init__(self, parent, source_elem, elem, driver, multiplier=1.0, use_cdp=False):
        self._is_real_elem = False
        self._use_cdp = use_cdp
        if not isinstance(source_elem, dict):
            # We need to also pass the _w3c flag otherwise the get_attribute for thing like html or text is messed up
            if old_selenium:
                super(ai_elem, self).__init__(source_elem.parent, source_elem._id, w3c=source_elem._w3c)
            else:
                super(ai_elem, self).__init__(source_elem.parent, source_elem._id)
            self._is_real_elem = True
        self.driver = driver
        multiplier = max(1, multiplier)
        self.multiplier = multiplier
        self._text = elem.get('text', '')
        self._size = {'width': elem.get('width', 0)/multiplier, 'height': elem.get('height', 0)/multiplier}
        self._location = {'x': elem.get('x', 0)/multiplier, 'y': elem.get('y', 0)/multiplier}
        self._property = elem.get('class', '')
        self._rect = {}
        self.rect.update(self._size)
        self.rect.update(self._location)
        self._tag_name = elem.get('class', '')
        self._cx = elem.get('x', 0)/multiplier + elem.get('width', 0) / multiplier / 2
        self._cy = elem.get('y', 0)/multiplier + elem.get('height', 0) /multiplier / 2
        self._top_left = {'x': elem.get('x', 0)/multiplier, 'y': elem.get('y', 0)/multiplier}
        self._top_right = {'x': elem.get('x', 0)/multiplier + elem.get('width', 0)/multiplier, 'y': elem.get('y', 0)/multiplier}
        self._bottom_left = {'x': elem.get('x', 0)/multiplier, 'y': elem.get('y', 0)/multiplier + elem.get('height', 0)/multiplier}
        self._bottom_right = {'x': elem.get('x', 0)/multiplier + elem.get('width', 0)/multiplier, 'y': elem.get('y', 0)/multiplier + elem.get('height', 0)/multiplier}

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

    def drag(self, tx, ty, start_x_offset=0, start_y_offset=0):
        self.driver.execute_cdp_cmd('Input.dispatchMouseEvent',
                                    {'type': 'mousePressed', 'button': 'left', 'clickCount': 1, 'x': self._cx + start_x_offset,
                                     'y': self._cy + start_y_offset})
        time.sleep(0.07)
        self.driver.execute_cdp_cmd('Input.dispatchMouseEvent',
                                    {'type': 'mouseMoved', 'button': 'left', 'x': self._cx + start_x_offset + tx,
                                     'y': self._cy + start_y_offset + ty})
        time.sleep(0.15)
        self.driver.execute_cdp_cmd('Input.dispatchMouseEvent',
                                    {'type': 'mouseReleased', 'button': 'left', 'x': self._cx + start_x_offset + tx,
                                     'y': self._cy + start_y_offset + ty})

    def highlight(self):
        self.drag(self._bottom_right['x'] - self._top_left['x'], self._bottom_right['y'] - self._top_left['y'],
                  start_x_offset=(self._top_left['x'] - self._bottom_right['x']) / 2,
                  start_y_offset=(self._top_left['y'] - self._bottom_right['y']) / 2)


    def click(self, js_click=False, use_cdp=False):
        if self._is_real_elem == True and use_cdp == False and self._use_cdp == False:
            if not js_click:
                webdriver.remote.webelement.WebElement.click(self)
            else:
                # Multiplier needs to be undone as js doesn't care about it. only selenium/appium
                self.driver.execute_script('document.elementFromPoint(%d, %d).click();' % (int(self._cx), int(self._cy)))
        else:
            # Multiplier needs to be undone as js doesn't care about it. only selenium/appium
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', { 'type': 'mouseMoved', 'button': 'left', 'clickCount': 1, 'x': self._cx, 'y': self._cy})
            time.sleep(0.07)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', { 'type': 'mousePressed', 'button': 'left', 'clickCount': 1, 'x': self._cx, 'y': self._cy})
            time.sleep(0.15)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', { 'type': 'mouseReleased', 'button': 'left', 'clickCount': 1, 'x': self._cx, 'y': self._cy})

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

from selenium.webdriver.common.by import By
class By(By):
    AI = 'ai'