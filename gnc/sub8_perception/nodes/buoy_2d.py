#!/usr/bin/env python
import cv2
import numpy as np
import sys
import rospy
import image_geometry
import sub8_ros_tools
import tf
from sub8_vision_tools import machine_learning
import rospkg
import os
from collections import deque
from sub8_vision_tools import threshold_tools, rviz, ProjectionParticleFilter, MultiObservation
from sub8_msgs.srv import VisionRequest2DResponse, VisionRequest2D, VisionRequest, VisionRequestResponse
from std_msgs.msg import Header
from std_srvs.srv import SetBool, SetBoolResponse
from geometry_msgs.msg import Pose2D, PoseStamped, Pose, Point


class BuoyFinder:
    _min_size = 50

    def __init__(self):
        self.transformer = tf.TransformListener()
        rospy.sleep(2.0)

        self.search = False
        self.last_image = None
        self.last_draw_image = None
        self.last_image_time = None
        self.camera_model = None
        self.ppf = None
        self.multi_obs = None
        self.max_observations = 50
        self._id = 0  # Only for display

        self.rviz = rviz.RvizVisualizer()

        rospack = rospkg.RosPack()
        boost_path = os.path.join(
            rospack.get_path('sub8_perception'),
            'sub8_vision_tools',
            'machine_learning',
            'classifiers',
            'red_gentle_20tree_9depth.dic'
        )

        self.boost = cv2.Boost()
        rospy.loginfo("Loading boost")
        self.boost.load(boost_path)
        rospy.loginfo("Boost loaded")

        self.observations = {
            'red':deque(), 
            'yellow':deque(), 
            'green':deque()
        }
        self.pose_pairs = {
            'red':deque(), 
            'yellow':deque(), 
            'green':deque()
        }
        self.buoys = {
            'green': '/color/buoy/green',
            'red': '/color/buoy/red',
            'yellow': '/color/buoy/yellow',
        }
        self.last_t = {
            'green': None,
            'red': None,
            'yellow': None
        }

        # For displaying each buoy in rviz
        self.draw_colors = {
            'green': (0.0, 1.0, 0.0, 1.0),
            'red': (1.0, 0.0, 0.0, 1.0),
            'yellow': (1.0, 1.0, 0.0, 1.0),
        }
        self.visual_id = {
            'green': 0,
            'red': 1,
            'yellow': 2,
        }

        self.image_sub = sub8_ros_tools.Image_Subscriber('/stereo/right/image_raw', self.image_cb)
        self.image_pub = sub8_ros_tools.Image_Publisher('/vision/buoy_2d/target_info')
        self.mask_pub = sub8_ros_tools.Image_Publisher('/vision/buoy_2d/mask')

        # Occasional status publisher
        self.timer = rospy.Timer(rospy.Duration(1), self.publish_target_info)

        self.toggle = rospy.Service('vision/buoys/search', SetBool, self.toggle_search)
        self.pose2d_service = rospy.Service('vision/buoys/2D', VisionRequest2D, self.request_buoy)
        self.pose_service = rospy.Service('vision/buoys/pose', VisionRequest, self.request_buoy3d)

    def toggle_search(self, srv):
        if srv.data:
            rospy.loginfo("MARKER - Looking for markers now.")
            self.search = True
        else:
            rospy.loginfo("MARKER - Done looking for markers.")
            self.search = False

        return SetBoolResponse(success=srv.data)

    def request_buoy(self, srv):
        print 'requesting', srv
        timestamp = self.last_image_time
        response = self.find_single_buoy(np.copy(self.last_image), timestamp, srv.target_name)

        if response is False:
            print 'did not find'
            resp = VisionRequest2DResponse(
                header=sub8_ros_tools.make_header(frame='/stereo_front/right'),
                found=False
            )

        else:
            # Fill in
            center, radius = response
            resp = VisionRequest2DResponse(
                header=Header(stamp=timestamp, frame_id='/stereo_front/right'),
                pose=Pose2D(
                    x=center[0],
                    y=center[1],
                ),
                max_x=self.last_image.shape[0],
                max_y=self.last_image.shape[1],
                camera_info=self.image_sub.camera_info,
                found=True
            )
        return resp

    def request_buoy3d(self, srv):
        print "Requesting 3d pose"

        if (len(self.observations[srv.target_name]) > 5) and self.multi_obs is not None:
            estimated_pose = self.multi_obs.multilaterate(self.observations[srv.target_name], self.pose_pairs[srv.target_name])

            self.rviz.draw_sphere(estimated_pose, color=self.draw_colors[srv.target_name], 
                scaling=(0.5, 0.5, 0.5), frame='/map', _id=self.visual_id[srv.target_name])

            resp = VisionRequestResponse(
                pose=PoseStamped(
                    header=Header(stamp=self.last_image_time, frame_id='/map'),
                    pose=Pose(
                        position=Point(*estimated_pose)
                    )
                ),
                found=True
            )
        else:
            if len(self.observations[srv.target_name]) <= 5:
                rospy.logerr("Did not attempt search because we did not have enough observations ({})".format(self.observations[srv.target_name]))
            else:
                rospy.logerr("Did not attempt search because buoys_2d was not fully initialized")

            resp = VisionRequestResponse(
                pose=PoseStamped(
                    header=Header(stamp=self.last_image_time, frame_id='/map'),
                ),
                found=False
            )
        return resp

    def publish_target_info(self, *args):
        if not self.search or self.last_image is None:
            return

        self.find_buoys(np.copy(self.last_image), self.last_image_time)
        if self.last_draw_image is not None:
            self.image_pub.publish(self.last_draw_image)

    def image_cb(self, image):
        '''Hang on to last image'''
        self.last_image = image
        self.last_image_time = self.image_sub.last_image_time
        if self.camera_model is None:
            if self.image_sub.camera_info is None:
                return

            self.camera_model = image_geometry.PinholeCameraModel()
            self.camera_model.fromCameraInfo(self.image_sub.camera_info)
            self.multi_obs = MultiObservation(self.camera_model)

    def ncc(self, image, mean_thresh, scale=15):
        '''Compute normalized cross correlation w.r.t a shadowed pillbox fcn

        The expected scale will vary, so we don't cache it
        '''
        kernel = np.ones((scale, scale)) * -1
        midpoint = (scale // 2, scale // 2)
        cv2.circle(kernel, midpoint, midpoint[0], 1, -1)

        mean, std_dev = cv2.meanStdDev(image)

        # Check if the scene is brighter than our a priori target
        if mean > mean_thresh:
            kernel = -kernel

        normalized_cross_correlation = cv2.filter2D((image - mean) / std_dev, -1, kernel)
        renormalized = normalized_cross_correlation
        return renormalized

    def get_biggest(self, contours):
        if len(contours) > 0:
            cnt = max(contours, key=cv2.contourArea)
            area = cv2.contourArea(cnt)
            if area > self._min_size:
                M = cv2.moments(cnt)
                cx = int(M['m10'] / M['m00'])
                cy = int(M['m01'] / M['m00'])
                tpl_center = (int(cx), int(cy))
                return cnt, tpl_center, area
        else:
            return None

    def find_single_buoy(self, img, timestamp, buoy_type):
        assert buoy_type in self.buoys[buoy_type], "Buoys_2d does not know buoy color: {}".format(buoy_type)
        max_area = 0
        best_ret = None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        # Segmentation here (machine learning right now)
        # some_observations = machine_learning.boost.observe(img)
        # prediction2 = [int(x) for x in [self.boost.predict(obs) for obs in some_observations]]
        # mask = np.reshape(prediction2, img[:, :, 2].shape).astype(np.uint8) * 255

        low = np.array(rospy.get_param(self.buoys[buoy_type] + '/hsv_low')).astype(np.int32)
        high = np.array(rospy.get_param(self.buoys[buoy_type] + '/hsv_high')).astype(np.int32)
        mask = cv2.inRange(hsv, low, high)

        rospy.sleep(.5)

        kernel = np.ones((13,13),np.uint8)
        mask = cv2.dilate(mask, kernel, iterations = 1)
        mask = cv2.erode(mask, kernel, iterations = 1)

        self.mask_pub.publish(np.dstack([mask] * 3))

        contours, _ = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

        ret = self.get_biggest(contours)
        if ret is None:
            return

        contour, tuple_center, area = ret
        if area > max_area:
            max_area = area
            best_ret = ret

        if best_ret is None:
            return False

        contour, tuple_center, area = best_ret
        true_center, rad = cv2.minEnclosingCircle(contour)

        if self.camera_model is not None:

            if not self.sanity_check(tuple_center, timestamp):
                return False

            (t, rot_q) = self.transformer.lookupTransform('/map', '/stereo_front/right', timestamp)
            trans = np.array(t)
            R = sub8_ros_tools.geometry_helpers.quaternion_matrix(rot_q)

            # self.rviz.draw_ray_3d(tuple_center, self.camera_model, self.draw_colors[buoy_type], frame='/stereo_front/right', _id=self._id + 100)
            # self._id += 1
            # if self._id >= self.max_observations * 3:
            #     self._id = 0

            if (self.last_t[buoy_type] is None) or (np.linalg.norm(trans - self.last_t[buoy_type]) > 0.3):
                self.last_t[buoy_type] = trans
                self.observations[buoy_type].append(true_center)
                self.pose_pairs[buoy_type].append((t, R))

            if len(self.observations[buoy_type]) > 5:
                est = self.multi_obs.multilaterate(self.observations[buoy_type], self.pose_pairs[buoy_type])
                
                self.rviz.draw_sphere(est, color=self.draw_colors[buoy_type], 
                    scaling=(0.5, 0.5, 0.5), frame='/map', _id=self.visual_id[buoy_type])

            if len(self.observations[buoy_type]) > self.max_observations:
                self.observations[buoy_type].popleft()
                self.pose_pairs[buoy_type].popleft()

        return tuple_center, rad

    def find_buoys(self, img, timestamp):
        draw_image = np.copy(img)

        # This is only run if buoy_type is not None
        for buoy_name in self.buoys.keys():
            #rospy.loginfo("BUOY - Looking for {}".format(buoy_name))
            result = self.find_single_buoy(img, timestamp, buoy_name)
            if not result:
                continue

            center, rad = result
            cv2.circle(draw_image, center, int(rad), (255, 255, 0), 2)
            font = cv2.FONT_HERSHEY_SIMPLEX
            cv2.putText(draw_image, '{}'.format(buoy_name), center, font, 0.8, (20, 20, 240), 1)

        self.last_draw_image = np.copy(draw_image)

    def sanity_check(self, coordinate, timestamp):
        '''
        Check if the observation is unreasonable. More can go here if we want.
        '''
        sane = True
        if np.linalg.norm(self.transformer.lookupTwist('/map', '/stereo_front/right', timestamp, rospy.Duration(.5))) > 1:
            rospy.logerr("BUOY - Moving too fast. Not observing buoy.")
            sane = False

        return sane

def main(args):
    bf = BuoyFinder()
    rospy.spin()

if __name__ == '__main__':
    rospy.init_node('orange_pipe_vision')
    main(sys.argv)
