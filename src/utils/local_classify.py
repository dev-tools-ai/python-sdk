import os
import cv2
import base64
import numpy as np
import logging
import glob
import json
import hashlib

from devtools_ai.utils.network_utils import NetworkUtils

log = logging.getLogger(__name__)

# Converts a b64 image to a cv2 image
def b642cv2(b64img):
    img_bytes = base64.b64decode(b64img)
    np_arr = np.fromstring(img_bytes, np.uint8)
    cv_img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    return cv_img

class LocalClassifier:
    def __init__(self, url, api_key, local_cache_directory=None, template_match_threshold=0.998):
        self.api_key = api_key
        if local_cache_directory is None:
            if os.name == 'nt':
                local_cache_directory = os.path.expanduser('~\\AppData\\Local\\.smartdriver\\cache')
            else:
                local_cache_directory = os.path.expanduser(f'~/.smartdriver/cache/{api_key}')
        else:
            local_cache_directory = os.path.expanduser(local_cache_directory)
        self.local_cache_directory = local_cache_directory
        os.makedirs(local_cache_directory, exist_ok=True)
        self.network_utils = NetworkUtils(url)
        self.elements_data_filename = os.path.join(local_cache_directory, 'element_data.json')
        self.elements_data = {}
        self.template_match_threshold = template_match_threshold
        self.load_known_elements()

    def load_known_elements(self):
        if os.path.exists(self.elements_data_filename):
            with open(self.elements_data_filename, 'r') as f:
                self.elements_data = json.load(f)

    def save_elements_data(self):
        with open(self.elements_data_filename, 'w') as f:
            json.dump(self.elements_data, f)

    def get_template_path(self, template_uuid):
        return os.path.join(self.local_cache_directory, template_uuid)

    def cache_templates_for_element(self, element_name):
        try:
            log.debug(f'Caching template for element {element_name}')
            data = {'api_key': self.api_key, 'label': element_name}
            element_template_data = self.network_utils.make_json_post_request('/get_element_template_data', data, 'Error getting element template data', 10)
            if element_template_data['success']:
                self.elements_data[element_name] = element_template_data
                log.debug(f'Found {len(element_template_data["templates"])} templates for element {element_name}')
                for template in element_template_data['templates']:
                    template_uuid = template['template_uuid']
                    if not os.path.exists(os.path.join(self.local_cache_directory, template_uuid)):
                        log.debug(f'caching template {template_uuid}')
                        data = {
                            'screenshot_uuid': template_uuid,
                            'api_key': self.api_key,
                            'label': element_name
                        }
                        template_image = self.network_utils.make_json_post_request('/retrieve_element', data, 'Error retrieving element', 30)
                        if template_image['success']:
                            with open(self.get_template_path(template_uuid), 'wb') as f:
                                f.write(template_image['screenshot_b64'].encode('utf-8'))
                            cv2.imwrite(self.get_template_path(template_uuid) + '.png', b642cv2(template_image['screenshot_b64']))
                    else:
                        log.debug(f'Template already cached {template_uuid}')
        except Exception as e:
            log.exception(e)
            log.error(f'Error caching template for element {element_name}: {e}')
        self.save_elements_data()

    def classify_element(self, element_name, screenshot_b64):
        #classify logic
        try:
            if element_name not in self.elements_data:
                log.info(f'Element {element_name} not found in local cache, using online prediction')
                return None
            res = self.do_template_match(element_name, screenshot_b64)
            img = b642cv2(screenshot_b64)
            predicted_element, score = self.find_best_candidate_wrapper(res, self.elements_data[element_name], img)
            if predicted_element is not None:
                log.info(f'Classified locally element {element_name} with score {score}')
            else:
                log.info(f'Could not classify locally element {element_name} using web prediction')
            return predicted_element
        except Exception as e:
            log.exception(e)
            log.error(f'Error doing local classification of element {element_name}')
            return None

    def find_best_candidate_wrapper(self, res, element_data, img):
        # naive matching for now, TODO implement geographic matching based on element data
        res['action_infos'] = element_data['action_infos']
        pred_element, score = self.find_best_candidate(res, img, threshold=self.template_match_threshold)
        return pred_element, score

    def do_template_match(self, element_name, screenshot_b64):
        templates_uuids = [data['template_uuid'] for data in self.elements_data[element_name]['templates']]
        templates = []
        for template_uuid in templates_uuids:
            if os.path.exists(os.path.join(self.local_cache_directory, template_uuid)):
                with open(os.path.join(self.local_cache_directory, template_uuid), 'rb') as f:
                    template_b64 = f.read().decode('utf-8')
                    template = b642cv2(template_b64)
                    templates.append(template)

        img = b642cv2(screenshot_b64)
        res = {
            'boxes': [],
            'scores': []
        }

        for template in templates:
            log.debug(f'Performing template match for element {element_name}')
            boxes, scores = self.template_match_core_multi(img, template)
            for box, score in zip(boxes, scores):
                if score > self.template_match_threshold:
                    res['boxes'].append(box)
                    res['scores'].append(score)
        return res

    def template_match_core_multi(self, img, template):
        method = cv2.TM_SQDIFF_NORMED
        res = cv2.matchTemplate(img, template, method)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        h, w, c = template.shape

        boxes = []
        scores = []
        coords = np.column_stack(np.where(res <= min_val * 1.03))
        for coord in coords:
            top_left = coord
            box = {'x': top_left[1], 'y': top_left[0], 'width': w, 'height': h}
            score = res[coord[0], coord[1]]
            boxes.append(box)
            scores.append(1.0 - score)
        return boxes, scores

    def cdist(self, centers_a, center_b):
        return [np.linalg.norm(np.array(center_a) - np.array(center_b)) for center_a in centers_a]

    def box_in_screenshot(self, box, shape):
        x = box['x']
        y = box['y']
        w = box['width']
        h = box['height']
        return x >= 0 and y >= 0 and x + w <= shape[1] and y + h <= shape[0]

    def get_boxes_centers(self, boxes, img_shape):
        box_centers = []
        for box in boxes:
            h, w, c = img_shape
            normalized_box = {'x': box['x'] / w, 'y': box['y'] / h, 'width': box['width'] / w,
                              'height': box['height'] / h}
            vec_box_center = np.array(
                [normalized_box['x'] + normalized_box['width'] / 2, normalized_box['y'] + normalized_box['height'] / 2])
            box_centers.append(vec_box_center)
        return np.array(box_centers)

    def find_best_candidate(self, res, img, mode='geographic', threshold=0.9999):
        #TODO use image shape
        score = None
        log.debug(f'Finding best candidate from results')
        if res is not None:
            oscores = res['scores']
            oboxes = res['boxes']
            img_shape = img.shape

            scores = []
            boxes = []
            for score, box in zip(oscores, oboxes):
                if self.box_in_screenshot(box, img_shape):
                    boxes.append(box)
                    scores.append(score)

            log.debug(f'Length of scores: {len(scores)}')
            if len(scores) > 0 and len(boxes) > 0:
                max_score = max(scores)
                if mode == 'geographic':
                    cut_off = threshold
                    geographic_scores = []
                    geographic_boxes = []
                    for score, box in zip(scores, boxes):
                        if score >= cut_off:
                            geographic_scores.append(score)
                            geographic_boxes.append(box)

                        vec_action_infos_centers = self.get_action_infos_centers(res['action_infos'])
                        vec_box_centers = self.get_boxes_centers(geographic_boxes, img_shape)

                        dists = []
                        for idx_box in range(len(vec_box_centers)):
                            for idx_action_info in range(len(vec_action_infos_centers)):
                                dist = self.cdist([vec_box_centers[idx_box]], vec_action_infos_centers[idx_action_info])
                                dists.append((idx_box, dist))
                        amin = min(dists, key=lambda el: el[1])[0]
                        best_geographic_box = geographic_boxes[amin]
                        best_geographic_score = geographic_scores[amin]

                        predicted_element = best_geographic_box
                        score = best_geographic_score
                        log.debug(f'Found a best geographic match {best_geographic_score} {best_geographic_box}')
            else:
                predicted_element = None
                score = None
        else:
            predicted_element = None
            score = None
        return predicted_element, score

    def get_action_infos_centers(self, action_infos):
        entity_centers = []
        for action_info in action_infos:
            entity = {'x': action_info['matched_entity']['x'] / action_info['matched_entity']['img_w'],
                      'y': action_info['matched_entity']['y'] / action_info['matched_entity']['img_h'],
                      'width': action_info['matched_entity']['width'] / action_info['matched_entity']['img_w'],
                      'height': action_info['matched_entity']['height'] / action_info['matched_entity']['img_h']}
            entity_centers.append(np.array([entity['x'] + entity['width'] / 2, entity['y'] + entity['height'] / 2]))
        return np.array(entity_centers)