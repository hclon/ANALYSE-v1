import ast, datetime, re, math

from xmodule.modulestore.django import modulestore
from xmodule.course_module import CourseDescriptor
from xmodule.video_module.video_module import VideoDescriptor
import xmodule.graders as xmgraders

from submissions import api as sub_api

from student.models import anonymous_id_for_user
from courseware.models import StudentModule
from student.models import CourseEnrollment, CourseAccessRole
from track.models import TrackingLog
#Codigo JaviOrcoyen
from models import LastKnownTrackingLog
from models import CourseVideos
from django.core.exceptions import ObjectDoesNotExist
from django.contrib.auth.models import User

from django.utils import timezone
from django.test.client import RequestFactory
from django.db import transaction
from django.db.models import Q

from contextlib import contextmanager
from django.conf import settings

from courseware.module_render import get_module_for_descriptor
from courseware.model_data import FieldDataCache

from eventtracking import tracker 
from opaque_keys.edx.locations import SlashSeparatedCourseKey, Location
from opaque_keys.edx.locator import CourseLocator, BlockUsageLocator
from instructor.utils import get_module_for_student

from operator import truediv
import json
from apiclient.discovery import build
import logging

log = logging.getLogger(__name__)

def get_courses_list():
    """
    Return a list with all course modules
    """
    return modulestore().get_courses()
    
    
def get_course_key(course_id):
    """
    Return course opaque key from olf course ID
    """
    
    #print 'ID'
    #print course_id
    # Get course opaque key
    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)
    return course_key

def get_course_module(course_key):
    """
    Return course module 
    """
    # Get course module
    course = modulestore().get_course(course_key)
    return course


def get_course_struct(course):
    """
    Return a dictionary with the course structure of all chapters, sequentials,
    and verticals modules with their names and usage_keys
    
    course: course module
    
    """
    course_key = course.id
    course_struct = {'id': course_key,
                     'name': course.display_name_with_default,
                     'chapters': [] }
    # Chapters
    for chapter in course.get_children():
        if chapter.category == 'chapter':
            released = (timezone.make_aware(datetime.datetime.now(), timezone.get_default_timezone()) > 
                              chapter.start)
            chapter_struct = {'id': chapter.location,
                                         'name': chapter.display_name_with_default,
                                         'sequentials': [],
                                         'released': released }
            chapter_graded = False
            # Sequentials
            for sequential in chapter.get_children():
                if sequential.category == 'sequential':
                    seq_struct = {'id': sequential.location,
                                          'name': sequential.display_name_with_default,
                                          'graded': sequential.graded,
                                          'verticals': [],
                                          'released':released }
                    if seq_struct['graded']:
                        chapter_graded = True
                    # Verticals
                    for vertical in sequential.get_children():
                        if vertical.category == 'vertical':
                            vert_struct = {'id': vertical.location,
                                                   'name': vertical.display_name_with_default,
                                                   'graded': vertical.graded,
                                                   'released': released }
                            seq_struct['verticals'].append(vert_struct)
                    chapter_struct['sequentials'].append(seq_struct)
            chapter_struct['graded'] = chapter_graded
            course_struct['chapters'].append(chapter_struct)
    
    return course_struct


def get_course_blocks(course):
    blocks = {}
    
    for chapter in course.get_children():
        for seq in chapter.get_children():
            for vert in seq.get_children():
                for block in vert.get_children():
                    blocks[block.location.block_id] = {'chapter':chapter.location,
                                                                         'sequential':seq.location,
                                                                         'block':block.location}
    return blocks


def get_course_grade_cutoff(course):
    return course.grade_cutoffs['Pass']


def get_course_students(course_key):
    """
    Obtain students id in the course and return their User object
    """
    students_id = (CourseEnrollment.objects
        .filter(course_id=course_key)
        .values_list('user_id', flat=True))
    students = User.objects.filter(id__in=students_id)
    return students

def dump_full_grading_context(course):
    """
    Render information about course grading context
    Returns all sections    
    """
    
    weight_subs = []
    subsections = []
    # Get sections in each weighted subsection (Homework, Lab or Exam)
    if isinstance(course.grader, xmgraders.WeightedSubsectionsGrader):
        gcontext = course.grading_context['graded_sections']
        for subgrader, category, weight in course.grader.sections:
            # Add weighted section to the list
            weight_subs.append({'type':subgrader.type, 'category':category, 'weight':weight})
            
            ## RE-CHECK: SECTIONS IN GRAD_CONTEXT BUT NOT IN SUBGRADERS
            if gcontext.has_key(category):
                for i in range(max(subgrader.min_count, len(gcontext[category]))):
                    if subgrader.min_count > 1:
                        label = subgrader.short_label + ' ' + str(subgrader.starting_index + i)
                    else:
                        label = subgrader.short_label
                    if i < len(gcontext[category]):
                        section_descriptor = gcontext[category][i]['section_descriptor']
                        problem_descriptors = gcontext[category][i]['xmoduledescriptors']
                        # See if section is released
                        released = (timezone.make_aware(datetime.datetime.now(), timezone.get_default_timezone()) > 
                                          section_descriptor.start)
                        
                        subsections.append({'category':category,
                                              'label':label,
                                              'released':released,
                                              'name': section_descriptor.display_name,
                                              'problems': problem_descriptors,
                                              'max_grade': section_max_grade(course.id, problem_descriptors)})
                    else:
                        subsections.append({'category':category,
                                              'label':label,
                                              'released':False,
                                              'name': category + ' ' + str(subgrader.starting_index + i) + ' Unreleased',
                                              'problems': [],
                                              'max_grade': 0})
            else:
                # Subsection Unreleased
                for i in range(subgrader.min_count):
                    subsections.append({'category':category,
                                          'label':subgrader.short_label + ' ' + str(subgrader.starting_index + i),
                                          'released':False,
                                          'name': category + ' ' + str(subgrader.starting_index + i) + ' Unreleased',
                                          'problems': [],
                                          'max_grade': 0})
                            
    elif isinstance(course.grader, xmgraders.SingleSectionGrader):
        # Single section
        gcontext = course.grading_context['graded_sections']
        singlegrader = course.grader
        # Add weighted section to the list
        weight_subs.append({'type':singlegrader.type, 'category':singlegrader.category, 'weight':1})
        
        if gcontext.has_key(singlegrader.category):
            section_descriptor = gcontext[singlegrader.category][0]['section_descriptor']
            problem_descriptors = gcontext[singlegrader.category][0]['xmoduledescriptors']
            # See if section is released
            released = (timezone.make_aware(datetime.datetime.now(), timezone.get_default_timezone()) > 
                              section_descriptor.start)
            subsections.append({'category':singlegrader.category,
                                'label':singlegrader.short_label,
                                'released':True,
                                'name': section_descriptor.display_name,
                                'problems': problem_descriptors,
                                'max_grade': section_max_grade(course.id, problem_descriptors)})
        else:
            # Subsection Unreleased
            subsections.append({'category':singlegrader.category,
                                'label':singlegrader.short_label,
                                'released':False,
                                'name': category + ' Unreleased',
                                'problems': [],
                                'max_grade': 0})
        
    dump_full_graded = {'weight_subsections':weight_subs, 'graded_sections':subsections}
    return dump_full_graded

        
def section_max_grade(course_key, problem_descriptors):
    """
    Return max grade a student can get in a series of problem descriptors
    """
    max_grade = 0
   
    students_id = CourseEnrollment.objects.filter(course_id=course_key)
    if students_id.count() == 0:
        return max_grade
       
    instructors = CourseAccessRole.objects.filter(role='instructor')
    if instructors.count() > 0:
        instructor = User.objects.get(id=instructors[0].user.id)
    else:
        ## TODO SEND WARNING BECAUSE COURSE HAVE NO INSTRUCTOR
        instructor = students_id[0].user

    for problem in problem_descriptors:
        score = get_problem_score(course_key, instructor, problem)
        if score is not None and score[1] is not None:
            max_grade += score[1]
                
    return max_grade      


def is_problem_done(course_key, user, problem_descriptor):
    """
    Return if problem given is done
    
    course_key: course opaque key
    user: a Student object
    problem_descriptor: an XModuleDescriptor
    """
    problem_grade = get_problem_score(course_key, user, problem_descriptor)[0]  # Get problem score (dont care total)
    if problem_grade:
        return True
    else:
        return False  # StudentModule not created yet (not done yet) or problem not graded
    
    
def get_problem_score(course_key, user, problem_descriptor):
    """
    Return problem score as a tuple (score, total).
    Score will be None if problem is not yet done.
    Based on courseware.grades get_score function.
    
    course_key: course opaque key
    user: a Student object
    problem_descriptor: an XModuleDescriptor
    """
    # some problems have state that is updated independently of interaction
    # with the LMS, so they need to always be scored. (E.g. foldit.)
    if problem_descriptor.always_recalculate_grades:
        problem_mod = get_module_for_student(user, problem_descriptor.location)
        if problem_mod is None:
            return (None, None)
        score = problem_mod.get_score()
        if score is not None:
            if score['total'] is None:
                return (None, problem_mod.max_score())
            elif score['score'] == 0:
                progress = problem_mod.get_progress()
                if progress is not None and progress.done():
                    return(score['score'], score['total'])
                else:
                    return (None, score['total']) 
            else:
                return(score['score'], score['total'])
        else:
            return (None, problem_mod.max_score())

    if not problem_descriptor.has_score:
        # These are not problems, and do not have a score
        return (None, None)

    try:
        student_module = StudentModule.objects.get(
            student=user,
            course_id=course_key,
            module_state_key=problem_descriptor.location)
    except StudentModule.DoesNotExist:
        student_module = None

    if student_module is not None and student_module.max_grade is not None:
        correct = student_module.grade
        total = student_module.max_grade
    else:
        # If the problem was not in the cache, or hasn't been graded yet,
        # we need to instantiate the problem.
        # Otherwise, the max score (cached in student_module) won't be available
        problem_mod = get_module_for_student(user, problem_descriptor.location)
        if problem_mod is None:
            return (None, None)

        correct = None
        total = problem_mod.max_score()

        # Problem may be an error module (if something in the problem builder failed)
        # In which case check if get_score() returns total value
        if total is None:
            score = problem_mod.get_score()
            if score is None:
                return (None, None)
            else:
                total = score['total']

    # Now we re-weight the problem, if specified
    weight = problem_descriptor.weight
    if weight is not None:
        if total == 0:
            return (correct, total)
        if correct is not None:
            correct = correct * weight / total
        total = weight
        
    return (correct, total)
   

### Codigo Javier Orcoyen, incluyo limit_id (busca a partir de un id, puede ser 0 para que busque todos los eventos)
def get_course_events_sql(course_key, student, limit_id):
    """
    Returns course events stored in tracking log for
    given course and student
    
    course_key: Course Opaque Key
    student: student object or student username as string
    """
    
    #Search for events in the track_trackinglog table with and id greater than limit_id (if limit_id=0, it will take all events)
    events = TrackingLog.objects.filter(username=student, id__gt=limit_id).order_by('time')
    # Filter events with course_key
    # TODO: More event types?
    filter_id = []
    for event in events:
        # Filter browser events with course_key
        if event.event_source == 'browser':
            if ((event.event_type == 'seq_goto' or 
                event.event_type == 'seq_next' or 
                event.event_type == 'page_close' or
                event.event_type == 'show_transcript' or
                event.event_type == 'load_video') and
                is_same_course(event.page, course_key)):#Codigo Gascon
                filter_id.append(event.id)

        # Filter server events with course_id
        elif event.event_source == 'server':
            split_url = filter(None, event.event_type.split('/'))
            #print 'split_sql'
            #print split_url
            #print event.event_type
            if len(split_url) != 0: 
                if split_url[-1] == 'logout':
                    filter_id.append(event.id)
                elif split_url[-1] == 'dashboard':
                    filter_id.append(event.id)
                elif is_same_course(event.event_type, course_key):#Codigo Gascon
                    filter_id.append(event.id)

    events = events.filter(id__in=filter_id)
    return events
    

### Codigo Javier Orcoyen, incluyo limit_id (busca a partir de un id, puede ser 0 para que busque todos los eventos) 
def get_course_access_events_sql(course_key, student, limit_id):
    """
    Returns course events stored in tracking log for
    course access
    course_key: Course Opaque Key
    student: student object or student username as string
    """
    
    #Search for events in the track_trackinglog table with and id greater than limit_id (if limit_id=0, it will take all events)
    events = TrackingLog.objects.filter(username=student, id__gt=limit_id).order_by('time')
    # Filter events with course_key
    filter_id = []
    for event in events:
        #print 'EVENTS EN GET_SQL_LOG'
        #print event 
        #print event.event_source
        #print 'events'
        #print event
        # Filter browser events with course_key
        if event.event_source == 'browser':
            if ((event.event_type == 'seq_prev' or 
                 event.event_type == 'seq_next' or
                 event.event_type == 'seq_goto') and
                 is_same_course(event.page, course_key)):#Codigo Gascon
                filter_id.append(event.id)
                
        # Filter server events with course_id
        elif event.event_source == 'server':
            split_url = filter(None, event.event_type.split('/'))
            if len(split_url) != 0: 
                if split_url[0] == 'courses':
                    if (is_same_course(event.event_type, course_key) and #Codigo Gascon
                        not is_xblock_event(event) and
                        get_locations_from_url(event.event_type)[1] is not None):
                        filter_id.append(event.id)
                            
    events = events.filter(id__in=filter_id)
    return events


### Codigo Javier Orcoyen, incluyo limit_id (busca a partir de un id, puede ser 0 para que busque todos los eventos)
def get_problem_history_sql(course_key, student, limit_id):
    """
    Returns course problem check events stored in tracking
    log 
    
    course_key: Course Opaque Key
    student: student object or student username as string
    """
    events = TrackingLog.objects.filter(Q(event_type='problem_check') | Q(event_type='problem_rescore'),
                                                          username=student.username,
                                                          event_source='server', id__gt=limit_id).order_by('time')
    
    filter_id = []
    
    for event in events:
        #print 'events get_problem_history_sql'
        #print event
        if is_problem_from_course(ast.literal_eval(event.event)['problem_id'], course_key):
            filter_id.append(event.id)
    
    events = events.filter(id__in=filter_id)
    return events


def is_problem_from_course(problem_id, course_key):
    ## TODO
    #CODIGO J. A. Gascon
    org = problem_id.split('+')[0].split(':')[1]
    course = problem_id.split('+')[1]

    return org == course_key.org and course == course_key.course
    

def is_same_course(course_1, course_2):
    """
    Compares 2 courses in any format (course_key, string,
    unicode, string or unicode with course on it, etc).
    Returns true if both are the same course and false in
    any other case
    """
    # Get course 1 key
    
    if (course_1.__class__ == SlashSeparatedCourseKey or
        course_1.__class__ == CourseLocator):
        course_1_key = course_1
    elif course_1.__class__ == str or course_1.__class__ == unicode:
        #print 'COURSE 1'
        #print course_1
        course_1_key = get_course_from_url(course_1) #AQUI ES DONDE FALLAAA
        #print course_1_key
        if course_1_key == None:
            #print 'FALSO NO SON IGUALES COURSE 1'
            return False
    else:
        # TODO: Check if is course module
        return False
    # Get course 2 key
    if (course_2.__class__ == SlashSeparatedCourseKey.__class__ or
        course_2.__class__ == CourseLocator):
        course_2_key = course_2
    elif course_2.__class__ == str or course_2.__class__ == unicode:
        #print 'COURSE 2'
        #print course_2
        course_2_key = get_course_from_url(course_2)
        if course_2_key == None:
            print 'FALSO NO SON IGUALES COURSE 2'
            return False
    else:
        print 'FALSEEE'
        # TODO: Check if is course module
        return False
    
    if course_1_key == course_2_key:
        print 'TRUE'
        return True
    else:
        return False
     
         
def get_course_from_url(url):
    """
    Return course key from an url that can be an old style
    course ID, or a url with old style course ID contained on
    it. Returns None if no course is find
    """
    #split_url = filter(None, url.split(':')[2].split('+'))
    split_url = filter(None, url.split('/'))
    #split_url2 = filter(None, url.split(':')[2].split('+'))
    #print 'SplitURL'
    #print split_url
    #print len(split_url)
    if len(split_url) < 3:
        # Wrong string
        return None
    elif len(split_url) == 3:
        # Old style course id
        """if url.split('/')[2]=='course-v1:edx+CS112+2015_T3' or url.split('/')[2]=='course-v1:edX+DemoX+Demo_Course'or url.split('/')[2]=='course-v1:edx+CS111+2015_T6':
            split_url = url.split('/')[2]
            print 'URL_MOD'
            print split_url
            return get_course_key(split_url)#Codigo Gascon
        else:"""
        split=split_url[1]
        split_url2 = filter(None, split.split('+'))
        #print 'URL_MOD2'
        #print split_url2
        if len(split_url2) == 3:
            #split_url = url.split('/')[2]
            #print 'URL_MOD'
            #print split_url2
            #print 'valida'
            #print split_url2
            return get_course_key(split)#Codigo Gascon
        else:
            #print 'no valida'
            #print split_url2
            return None
    else:
        # Search if course ID is contained in url
        course_index = 0
        for sect in split_url:
            course_index += 1
            if sect == 'courses':
                break
        if len(split_url) > course_index + 2:
            """print 'URL_sinMOD'
            print split_url
            if url.split('/')[2]=='course-v1:edx+CS112+2015_T3'or url.split('/')[2]=='course-v1:edX+DemoX+Demo_Course'or url.split('/')[2]=='course-v1:edx+CS111+2015_T6':
                split_url = url.split('/')[2]
                print 'URL_MOD'
                print split_url
                return get_course_key((split_url))
            else:"""
            
            split1=split_url[0]
            if split1 == 'courses':
                split=split_url[1]
                split_url2 = filter(None, split.split('+'))
                #print 'URL_MOD2'
                #print split_url2
            else:
                split=split_url[3]
                split_url2 = filter(None, split.split('+'))
                #print 'URL_MOD2 y BROWSERRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRR'
                #print split_url2
            if len(split_url2) ==3:
                #print 'valida'
                #print split_url2
                return get_course_key(split)#Codigo Gascon
            else:
                #print 'no valida'
                #print split_url2
                return None
            #return get_course_key('/'.join(split_url[course_index:course_index + 3]))
        
    return None


def get_locations_from_url(url, course_blocks=None):
    """
    Return sequential, chapter and course keys from
    given url if there is any
    
    Return (course key, chapter key, sequential key)
    """
    # Get route
    split_url = filter(None, url.split('/'))
    #print 'SPLIT URL LOCATIONS'
    #print split_url
    course_index = 0
    for sect in split_url:
        course_index += 1
        if sect == 'courses':
            break
    route = split_url[course_index:]
    #print 'SplitURL'
    #print split_url
    #print 'route'
    #print route
    if len(route) < 3:
        # No course in url
        return (None, None, None)
    else:
        course_key = get_course_key(route[0])#Codigo J. Antonio Gascon
        #print 'course_key'
        #print course_key
        #print '1) VAMOS A VER QUE HAY EN LOS ROUTESSSSSS (get_locations_from_url)'
        
        if route[1] != 'courseware':
            # No sequential or chapter
            if len(route) > 3 and route[1] == 'xblock':
                #print route[4]
                xblock_id = filter(None, route[2].split('@'))[-1]
                if course_blocks is None:
                    course_blocks = get_course_blocks(get_course_module(course_key))
                if course_blocks.has_key(xblock_id):
                    return (course_key, course_blocks[xblock_id]['chapter'], course_blocks[xblock_id]['sequential'])
                else:
                    #print "falla xblock_id"
                    return (course_key, None, None)
            else:
                #print "nada coincide con xblock"
                return (course_key, None, None)
        elif len(route) == 3:
            # Only chapter
            chapter_key = course_key.make_usage_key('chapter', route[2])
            return (course_key, chapter_key, None)
        else:
            split_comp= filter(None,route[3].split('-'))
            #print 'SPLIT COMP'
            #print split_comp
            #print len(split_comp)
            if len(split_comp)>1:
            # Chapter and sequential
                print 'CHILD'
                #print route[3]
                chapter_key = course_key.make_usage_key('chapter', route[2])
                return (course_key, chapter_key, None)
            else:
                print 'DENTRO COURSES'
                chapter_key = course_key.make_usage_key('chapter', route[2])
                sequential_key = course_key.make_usage_key('sequential', route[3])
                return (course_key, chapter_key, sequential_key)
                
            
 
def is_xblock_event(event):
    if event.event_source != 'server':
        return False

    # Get route
    split_url = filter(None, event.event_type.split('/'))
    course_index = 0
    for sect in split_url:
        course_index += 1
        if sect == 'courses':
            break
    route = split_url[course_index:]
    
    if len(route) > 3 and route[3] == 'xblock':
        return True
    else:
        return False


def compare_locations(loc1, loc2, course_key=None):
    """
    Compare if is same location in a certain course
    due to opaque keys sometimes comparisons are not ok
    """
    if loc1 == loc2: return True
    
    if course_key is None:
        if loc1.__class__ == Location or loc1.__class__ == BlockUsageLocator:
            course_key = loc1.course_key
        elif loc2.__class__ == Location or loc2.__class__ == BlockUsageLocator:
            course_key = loc2.course_key
        else:
            return False
        
    if loc1.__class__ == Location or loc1.__class__ == BlockUsageLocator:
        lockey1 = loc1
    elif loc1.__class__ == str or loc1.__class__ == unicode:
        lockey1 = course_key.make_usage_key_from_deprecated_string(loc1)
    else:
        return False
    
    if loc2.__class__ == Location or loc2.__class__ == BlockUsageLocator:
        lockey2 = loc2
    elif loc2.__class__ == str or loc2.__class__ == unicode:
        lockey2 = course_key.make_usage_key_from_deprecated_string(loc2)
    else:
        return False
        
    return lockey1.to_deprecated_string() == lockey2.to_deprecated_string()

"""
# OLD FUNCTION WITH API V2 Retrieve video-length via Youtube given its ID
def id_to_length(youtube_id):
  
    yt_service = gdata.youtube.service.YouTubeService()

    # Turn on HTTPS/SSL access.
    # Note: SSL is not available at this time for uploads.
    yt_service.ssl = True

    entry = yt_service.GetYouTubeVideoEntry(video_id=youtube_id)

    # Maximum video position registered in the platform differs around 1s
    # wrt youtube duration. Thus 1 is subtracted to compensate.
    return eval(entry.media.duration.seconds) - 1
"""

# Retrieve video-length via Youtube given its ID
def id_to_length(youtube_id):
  
    DEVELOPER_KEY = "AIzaSyBNs7EgFFJnzIse1ccGw6dbhIwd5Uycc4M"
    YOUTUBE_API_SERVICE_NAME = "youtube"
    YOUTUBE_API_VERSION = "v3"

    youtube = build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, 
      developerKey=DEVELOPER_KEY)
    
    #print(youtube_id)
    search_response = youtube.videos().list(
      id=youtube_id,
      part="contentDetails",
      maxResults=1
    ).execute()
    #print 'SEARCH RESPONSE'
    #print search_response
    
    
    #self.api_key = configuration.get_config().get('google', 'api_key', None)
    #duration = -1
    """if self.api_key is None:
        return duration"""

    video_file = None
    
    #Codigo Jose A. Gascon
    video_duration = search_response['items'][0]["contentDetails"]["duration"]
    
    """try:
        video_url = "https://www.googleapis.com/youtube/v3/videos?id={0}&part=contentDetails&key={1}".format(
                    youtube_id)
        video_file = urllib.urlopen(video_url)
        content = json.load(video_file)
        items = content.get('items', [])
        
        if len(items) > 0:
            duration_str = items[0].get(
                'contentDetails', {'duration': 'MISSING_CONTENTDETAILS'}
            ).get('duration', 'MISSING_DURATION')
            matcher = re.match(r'PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?', duration_str)
            if not matcher:
                log.error('Unable to parse duration returned for video %s: %s', youtube_id, duration_str)
            else:
                duration_secs = int(matcher.group('hours') or 0) * 3600
                duration_secs += int(matcher.group('minutes') or 0) * 60
                duration_secs += int(matcher.group('seconds') or 0)
                duration = duration_secs
        else:
            log.error('Unable to find items in response to duration request for youtube video: %s', youtube_id)
    except Exception:  # pylint: disable=broad-except
        log.exception("Unrecognized response from Youtube API")
    finally:
        if video_file is not None:
            video_file.close()        
    return duration
    #print(video_duration)
    #print("\n\n\n")
    """
    duration_iso_8601 = ''
    
    m = re.match('PT((?P<hours>[0-9]{1,2})H)?((?P<minutes>[0-9]{1,2})M)?((?P<seconds>[0-9]{1,2})S)?', video_duration)
    
    #m = re.match('PT((?P<hours>[0-9]{1,2})H)?((?P<minutes>[0-9]{1,2})M)?((?P<seconds>[0-9]{1,2})S)?',)
    hours = int(m.group('hours')) if m.group('hours') is not None else 0
    minutes = int(m.group('minutes')) if m.group('minutes') is not None else 0
    seconds = int(m.group('seconds')) if m.group('seconds') is not None else 0

    # Maximum video position registered in the platform differs around 1s
    # wrt youtube duration. Thus 1 is subtracted to compensate.
    #return eval(entry.media.duration.seconds) - 1
    return hours*60*60+minutes*60+seconds - 1
    

# Returns info of videos in course.
# Specifically returns their names, durations and module_ids
def get_info_videos(course):
    video_descriptors = list_video_descriptors(course)
    video_names = []
    youtube_ids = []
    video_durations = []
    video_module_ids = []
    #Codigo Jose A. Gascon
    for video_descriptor in video_descriptors:
        video_names.append(video_descriptor.display_name_with_default.encode('utf-8')) #__dict__['_field_data_cache']['display_name'].encode('utf-8'))
        #youtube_ids.append(video_descriptor.__dict__['_field_data_cache']['youtube_id_1_0'].encode('utf-8'))
        video_module_ids.append(video_descriptor.location)
        context = VideoDescriptor.get_context(video_descriptor)
        print 'context'
        #print context.type
        #print context
        #print context['transcripts_basic_tab_metadata']['video_url']['value']
        url=str(context['transcripts_basic_tab_metadata']['video_url']['value'])
        you_tubeid= url.split('/')[-1]
        you_tubeid=you_tubeid.replace("']","")
        print you_tubeid
        youtube_ids.append(you_tubeid)
    
    for youtube_id in youtube_ids:
        video_durations.append(float(id_to_length(youtube_id))) #float useful for video_percentages to avoid precision loss
    for k in range(len(video_names)):
        try:
            # Using get beceause there will only be one entry for each student and each course
            CourseVideos.objects.get(video_name=video_names[k], video_module_ids=video_module_ids[k],video_duration=video_durations[k])
        except ObjectDoesNotExist:
            CourseVideos.objects.create(video_name=video_names[k], video_module_ids=video_module_ids[k],  video_duration=video_durations[k])

    return (video_names, video_module_ids, video_durations)

# Returns info of videos in course.
# Specifically returns their names, durations and module_ids
def get_info_videos_descriptors(video_descriptors):
  
    video_names = []
    youtube_ids = []
    video_durations = []
    video_module_ids = []

    #Codigo Jose A. Gascon
    for video_descriptor in video_descriptors:
        #print 'video_FIELDS'
        #print video_descriptor.location
        #print 'video_descriptor'
        #print video_descriptor.__dict__['scope_ids']
        video_names.append(video_descriptor.display_name_with_default.encode('utf-8'))
        #print video_descriptor.__dict__['fields'].encode('utf-8')
        #youtube_ids.append(video_descriptor.__dict__['_field_data_cache']['youtube_id_1_0'].encode('utf-8'))
        video_module_ids.append(video_descriptor.location)
        context = VideoDescriptor.get_context(video_descriptor)
        #print 'context'
        #print context.type
        #print context
        #print context['transcripts_basic_tab_metadata']['video_url']['value']
        url=str(context['transcripts_basic_tab_metadata']['video_url']['value'])
        you_tubeid= url.split('/')[-1]
        you_tubeid=you_tubeid.replace("']","")
        #print you_tubeid
        youtube_ids.append(you_tubeid)
    print 'video_names'
    print video_names
    
    """POR SI ALGUNA VEZ HAY QUE CONECTARSE A MONGO
    from pymongo import MongoClient
    import sys
    # establish a connection to the database
    connection = MongoClient('mongodb://localhost:27017/')
    db = connection['edxapp']
    collection = db['modulestore.structures']
    #db=connection.edxapp
    #collection=db.modulestore.definitions
    #query=ObjectId("5652e10656c02c111a5cca6e")
    objeto=collection.find()
    print 'OBJETO'
    print objeto
    for item in collection.find({ "blocks.block_type" : "video" },{"fields":true,"youtube_id_1_0":true}):
        print item
    """
    #print "Videos:\n", "\n".join(youtube_ids), "\n"
    for youtube_id in youtube_ids:
        #print youtube_id
        video_durations.append(float(id_to_length(youtube_id))) #float useful for video_percentages to avoid precision loss
    for k in range(len(video_names)):
        try:
            # Using get beceause there will only be one entry for each student and each course
            CourseVideos.objects.get(video_name=video_names[k], video_module_ids=video_module_ids[k],video_duration=video_durations[k])
        except ObjectDoesNotExist:
            CourseVideos.objects.create(video_name=video_names[k], video_module_ids=video_module_ids[k],  video_duration=video_durations[k])
    return video_names, video_module_ids, video_durations

# Given a course_descriptor returns a list of the videos in the course
def list_video_descriptors(course_descriptor):
    video_descriptors = []
    for chapter in course_descriptor.get_children():
        for sequential_or_videosequence in chapter.get_children():
            for vertical_or_problemset in sequential_or_videosequence.get_children():
                for content in vertical_or_problemset.get_children():
                    if content.location.category == unicode('video'):
                        video_descriptors.append(content)
    return video_descriptors
   
# Determine how much NON-OVERLAPPED time of video a student has watched
# Codigo Javier Orcoyen
def video_len_watched_lastdate(course_key, student, video_module_id, last_date=None):
    # check there's an entry for this video  
    interval_start, interval_end = find_video_intervals(course_key, student, video_module_id, last_date)[0:2]
    disjointed_start = [interval_start[0]]
    disjointed_end = [interval_end[0]]
    # building non-crossed intervals
    for index in range(0,len(interval_start)-1):
        if interval_start[index+1] == disjointed_end[-1]:
            disjointed_end.pop()
            disjointed_end.append(interval_end[index+1])
            continue
        elif interval_start[index+1] > disjointed_end[-1]:
            disjointed_start.append(interval_start[index+1])
            disjointed_end.append(interval_end[index+1])
            continue
        elif interval_end[index+1] > disjointed_end[-1]:
            disjointed_end.pop()
            disjointed_end.append(interval_end[index+1])
    return [disjointed_start, disjointed_end]

# Determines NON-OVERLAPPED intervals from a set of intervals
def video_len_watched(interval_start, interval_end):

    disjointed_start = [interval_start[0]]
    disjointed_end = [interval_end[0]]
    # building non-crossed intervals
    for index in range(0,len(interval_start)-1):
        if interval_start[index+1] == disjointed_end[-1]:
            disjointed_end.pop()
            disjointed_end.append(interval_end[index+1])
            continue
        elif interval_start[index+1] > disjointed_end[-1]:
            disjointed_start.append(interval_start[index+1])
            disjointed_end.append(interval_end[index+1])
            continue
        elif interval_end[index+1] > disjointed_end[-1]:
            disjointed_end.pop()
            disjointed_end.append(interval_end[index+1])
    return disjointed_start, disjointed_end    


# Codigo Javier Orcoyen
def get_problem_intervals(student, problem_module_id, limit_id):

    INVOLVED_EVENTS = [
        'seq_goto',
        'seq_prev',
        'seq_next',
        'page_close'
    ]
    
    iter_problem_module_id = to_iterable_module_id(problem_module_id)
    #print 'problem_module_id'
    #print problem_module_id
    str1 = ';_'.join(x for x in iter_problem_module_id if x is not None)
    str2= str1.split('_')[-1]
    #print 'str1'
    #print str1
    #print 'str2'
    #print str2
    #org = problem_id.split('+')[0].split(':')[1]
    #str1=str5
    # str1 = ''.join([problem_module_id.DEPRECATED_TAG,':;_;_',str1]) 
    #DEPRECATED TAG i4x                str2 = ''.join([problem_module_id.DEPRECATED_TAG,':;_;_',str1])
    cond1 = Q(event_type__in=INVOLVED_EVENTS)
    cond2 = Q(event_type__contains = str2) & Q(event_type__contains = 'problem_get')
    shorlist_criteria = Q(username=student) & (cond1 | cond2)
    """print'shortlist'
    print shorlist_criteria
    print limit_id"""
    events = TrackingLog.objects.filter(shorlist_criteria, id__gt=limit_id)
    #print 'events in get_problem_intervals ANTES'
    #print events
    if events.filter(event_type__in=INVOLVED_EVENTS).count() <= 0:
        events = []
    #print 'events in get_problem_intervals'
    #print events
    return events


#Codigo Javier Orcoyen
def get_video_events(student, video_module_id, limit_id):
  
    INVOLVED_EVENTS = [
        'play_video',
        'pause_video',
        'speed_change_video',
        'seek_video'
    ]

    #shortlist criteria
    cond1 = Q(event_type__in=INVOLVED_EVENTS, event__contains=video_module_id.html_id())
    shorlist_criteria = Q(username=student) & cond1
    
    events = TrackingLog.objects.filter(shorlist_criteria, id__gt=limit_id)
    #print 'events get_video_events'
    #print events
    return events


#Codigo Javier Orcoyen
def get_video_intervals(student, video_module_id, last_date, limit_id):
    
    INVOLVED_EVENTS = [
        'play_video',
        'seek_video',
    ]
    str1=video_module_id.to_deprecated_string().replace('/',';_')
    print 'str1'
    print str1
    #shortlist criteria
    cond1   = Q(event_type__in=INVOLVED_EVENTS, event__contains=video_module_id.html_id())
    cond2_1 = Q(event_type__contains = video_module_id.to_deprecated_string().replace('/',';_'))
    cond2_2 = Q(event_type__contains='save_user_state', event__contains='saved_video_position')
    shorlist_criteria = Q(username=student) & (cond1 | (cond2_1 & cond2_2))
    #print'shortlist'
    #print shorlist_criteria
    #Search for events in the track_trackinglog table with and id greater than limit_id (if limit_id=0, it will take all events)
    events = TrackingLog.objects.filter(shorlist_criteria, id__gt=limit_id).order_by('time')
    #print 'events get_video_intervals'
    #print events
    if last_date is not None:
        events = events.filter(dtcreated__lte = last_date)
 
    return events


# Given a video descriptor returns ORDERED the video intervals a student has seen
# A timestamp of the interval points is also recorded.
### Codigo Javier Orcoyen, incluyo limit_id (busca a partir de un id, puede ser 0 para que busque todos los eventos)
def find_video_intervals(course_key, student, video_module_id, last_date = None):
    
    #event flags to check for duplicity
    play_flag = False # True: last event was a play_video
    seek_flag = False # True: last event was a seek_video
    saved_video_flag = False # True: last event was a saved_video_position
    
    interval_start = []
    interval_end = []
    vid_start_time = [] # timestamp for interval_start
    vid_end_time = []   # timestamp for interval_end
    
    events = get_new_module_events_sql(course_key, student, video_module_id, 'videoProgress2', last_date)
    
    if events is None:
        return [0], [0], [], []
    
    #if last_date is not None:
    #    events = events.filter(dtcreated__lte = last_date)
 
    if events.count() <= 0:
        # return description: [interval_start, interval_end, vid_start_time, vid_end_time]
        # return list types: [int, int, datetime.date, datetime.date]
        return [0], [0], [], []
    #guarantee the list of events starts with a play_video
    while events[0].event_type != 'play_video':
        events = events[1:]
        if len(events) < 2:
            return [0], [0], [], []
    for event in events:
        if event.event_type == 'play_video':
            if play_flag: # two consecutive play_video events. Second is the relevant one (loads saved_video_position).
                interval_start.pop() #removes last element
                vid_start_time.pop()
            if not seek_flag:
                interval_start.append(eval(event.event)['currentTime'])
                vid_start_time.append(event.time)
            play_flag = True
            seek_flag = False
            saved_video_flag = False
        elif event.event_type == 'seek_video':
            if seek_flag:
                interval_start.pop()
                vid_start_time.pop()
            elif play_flag:
                interval_end.append(eval(event.event)['old_time'])
                vid_end_time.append(event.time)
            interval_start.append(eval(event.event)['new_time'])
            vid_start_time.append(event.time)
            play_flag = False
            seek_flag = True
            saved_video_flag = False
        else: # .../save_user_state
            if play_flag:
                interval_end.append(hhmmss_to_secs(eval(event.event)['POST']['saved_video_position'][0]))
                vid_end_time.append(event.time)
            elif seek_flag:
                interval_start.pop()
                vid_start_time.pop()
            play_flag = False
            seek_flag = False
            saved_video_flag = True    
    interval_start = [int(math.floor(val)) for val in interval_start]
    interval_end   = [int(math.floor(val)) for val in interval_end]
    #remove empty intervals (start equals end) and guarantee start < end 
    interval_start1 = []
    interval_end1 = []
    vid_start_time1 = []
    vid_end_time1 = []
    for start_val, end_val, start_time, end_time in zip(interval_start, interval_end, vid_start_time, vid_end_time):
        if start_val < end_val:
            interval_start1.append(start_val)
            interval_end1.append(end_val)
            vid_start_time1.append(start_time)
            vid_end_time1.append(end_time)
        elif start_val > end_val: # case play from video end
            interval_start1.append(0)
            interval_end1.append(end_val)
            vid_start_time1.append(start_time)
            vid_end_time1.append(end_time)	    
    # sorting intervals
    if len(interval_start1) <= 0:
        return [0], [0], [], []
    [interval_start, interval_end, vid_start_time, vid_end_time] = zip(*sorted(zip(interval_start1, interval_end1, vid_start_time1, vid_end_time1)))
    interval_start = list(interval_start)
    interval_end = list(interval_end)
    vid_start_time = list(vid_start_time)
    vid_end_time = list(vid_end_time)
    
    # return list types: [int, int, datetime.date, datetime.date]
    return interval_start, interval_end, vid_start_time, vid_end_time


### Codigo Javier Orcoyen, incluyo limit_id (busca a partir de un id, puede ser 0 para que busque todos los eventos)
def get_video_events_interval(student, course_key, limit_id):
    INVOLVED_EVENTS = ['play_video',
                                       'seek_video',]
    #shortlist criteria
    cond1 = Q(event_type__in=INVOLVED_EVENTS, page__contains=course_key.html_id())
    cond2_1 = Q(event_type__contains = course_key.html_id())
    cond2_2 = Q(event_type__contains='save_user_state', event__contains='saved_video_position')
    shorlist_criteria = Q(username=student) & (cond1 | (cond2_1 & cond2_2))

    #Search for events in the track_trackinglog table with and id greater than limit_id (if limit_id=0, it will take all events)
    events = TrackingLog.objects.filter(shorlist_criteria, id__gt=limit_id).order_by('time')
    #print 'events get_video_events_interval'
    #print events
    if events.count() == 0:
        events = None

    return events

#### UTILS TO JS
def chapter_time_to_js(course_struct, students_time):
    """
    Formats time chapters data to send it to a javascript script
    """
    result = {}
    for st_id in students_time.keys():
        result[st_id] = []
        for chapter in course_struct:
            chapt_data = {'name': chapter['name'],
                          'total_time': round(truediv(students_time[st_id][chapter['id']]['time_spent'], 60),2)}
            graded_time = 0
            ungraded_time = 0
            for sequential in chapter['sequentials']:
                if sequential['graded']:
                    graded_time = (graded_time + 
                                   students_time[st_id][chapter['id']]['sequentials'][sequential['id']]['time_spent'])
                else:
                    ungraded_time = (ungraded_time + 
                                     students_time[st_id][chapter['id']]['sequentials'][sequential['id']]['time_spent'])
            
            chapt_data['graded_time'] = round(truediv(graded_time, 60),2)
            chapt_data['ungraded_time'] = round(truediv(ungraded_time, 60),2)
            result[st_id].append(chapt_data)
            
    return result


def students_to_js(students_user):
    result = []
    for user in students_user:
        result.append({'id':user.id, 'name':user.username })
    return result


def course_accesses_to_js(course_struct, students_course_accesses):
    """
    Formats course accesses data to send it to a javascript script
    """
    result = {}
    for st_id in students_course_accesses.keys():
        result[st_id] = []
        for chapter in course_struct:
            chapt_data = {'name': chapter['name'],
                          'accesses': students_course_accesses[st_id][chapter['id']]['accesses'],
                          'sequentials':[]}
            for sequential in chapter['sequentials']:
                seq_data = {'name': sequential['name'],
                            'accesses': students_course_accesses[st_id][chapter['id']]['sequentials'][sequential['id']]['accesses'],
                            'verticals':[]}
                for vertical in sequential['verticals']:
                    vert_data = {'name': vertical['name'],
                                 'accesses': students_course_accesses[st_id][chapter['id']]['sequentials'][sequential['id']]['verticals'][vertical['id']]['accesses']}
                    seq_data['verticals'].append(vert_data)
                chapt_data['sequentials'].append(seq_data)
            result[st_id].append(chapt_data)
    return result


### Codigo Javier Orcoyen, incluyo limit_id (busca a partir de un id, puede ser 0 para que busque todos los eventos) 
def get_all_events_sql(course_key, student, limit_id):
    
    #Search for events in the track_trackinglog table with and id greater than limit_id (if limit_id=0, it will take all events)
    events = TrackingLog.objects.filter(username=student, id__gt=limit_id).order_by('time')
    # Filter events with course_key
    filter_id = []
    for event in events:
        # Filter browser events with course_key
        if (event.event_source == 'browser'):
            if (is_same_course(event.page, course_key)):#Codigo Gascon
                #print 'browser'
                #print 'event_id1'
                #print event.id
                filter_id.append(event.id)        
        # Filter server events with course_id
        elif event.event_source == 'server':
            split_url = filter(None, event.event_type.split('/'))
            #print 'split_url'
            #print split_url
            if len(split_url) != 0: 
                if split_url[0] == 'courses':
                    #print event_type
                    #print course_key
                    if (is_same_course(event.event_type, course_key)):#Codigo Gascon
                        #print 'event_id1'
                        #print event.id
                        filter_id.append(event.id)
        
                                
    return events.filter(id__in=filter_id)



### Codigo Javier Orcoyen
def get_new_events_sql(course_key, student, indicator):

    lastKnownEventRegistered=0    
    
     
    #Check for the last known event of a student (student) for a certain course (course_key) in the LastKnownTrackingLog database
    try:   
        #Using get beceause there will only be one entry for each student and each course
        lastKnownEvent = LastKnownTrackingLog.objects.get(username=student, course_key=course_key, indicator=indicator)
    except ObjectDoesNotExist:
        lastKnownEvent = None
    
    #Check if there is not a last known event (first time it wont be) 
    if lastKnownEvent == None:
        #Take all course events of that student from the TrackingLog database as new events, no limit id 
        limitId = 0

    else:       
        #If there is a last known event registered, check for new events in the tracking log database
        lastKnownEventRegistered = 1
        #Check for new events in the TrackingLog database, those with id greater than the last known event id 
        limitId = lastKnownEvent.event_id

    #Get new events for time schedule
    if indicator == 'timeSchedule':
        events = get_all_events_sql(course_key, student, limitId)
    #Get new events for course accesses
    elif indicator == 'courseAccesses':
        events = get_course_access_events_sql(course_key, student, limitId)
    #Get new events for course spent time
    elif indicator == 'courseTime':
        events = get_course_events_sql(course_key, student, limitId)
    #Get new events for course problem progress
    elif indicator == 'problemProgress':
        events = get_problem_history_sql(course_key, student, limitId)
    #Get new events for course video progress the first time it needs to calculate new events
    elif indicator == 'videoProgress1':
        events = get_video_events_interval(student, course_key, limitId)
    
    #If there are new events, modify the lastKnownTracklog table to update/add the last known processed event
    if events:
        #Last known processed event of a student (student) for a certain course (course_key)           
        lastKnownProcessedEvent = events.reverse()[0]

        #Check if there is already a last known event registered in the database for a student in a certain course
        if lastKnownEventRegistered == 1:  
            #Update the last known processed event in the database (just update the id, the rest stays the same)
            lastKnownEvent.event_id = lastKnownProcessedEvent.id
            lastKnownEvent.save()
       
        else:  
            #Create the last kwnon processed event in the database
            LastKnownTrackingLog.objects.create(event_id=lastKnownProcessedEvent.id, username=lastKnownProcessedEvent.username, course_key=course_key, indicator=indicator)  
    else:
        events = None
    
    return events
     

### Codigo Javier Orcoyen
def get_new_module_events_sql(course_key, student, module_key, indicator, last_date):
    
    #print 'indicators'
    #print indicator
    lastKnownEventRegistered=0    
    
    #Check for the last known event of a student (student) for a certain course (course_key) in the LastKnownTrackingLog database
    try:   
        #Using get beceause there will only be one entry for each student and each course
        #print 'TRY LASTEVENT'
        lastKnownEvent = LastKnownTrackingLog.objects.get(username=student, course_key=course_key, module_key=module_key, indicator=indicator)
    except ObjectDoesNotExist:
        lastKnownEvent = None
    
    #Check if there is not a last known event (first time it wont be) 
    if lastKnownEvent == None:
        #print 'EXCEPTION LASTEVENT'
        #Take all course events of that student from the TrackingLog database as new events, no limit id 
        limitId = 0

    else:       
        #If there is a last known event registered, check for new events in the tracking log database
        lastKnownEventRegistered = 1
        #Check for new events in the TrackingLog database, those with id greater than the last known event id 
        limitId = lastKnownEvent.event_id

    #Get new events for video progress and video daily time, they call the same function
    if indicator == 'videoProgress2' or indicator == 'videoDailyTime' or indicator == 'videoTimeDistribution' or indicator == 'videoIntervalsRepetition' or indicator == 'videoTimeWatched':
        events = get_video_intervals(student, module_key, last_date, limitId)
    #Get new events for problem daily time
    elif indicator == 'problemDailyTime' or indicator == 'problemTimeDistribution':
        events = get_problem_intervals(student, module_key, limitId)
        #print "EVENTS IN"
        #print events
    #Get new events for video events
    elif indicator == 'videoEvents':
        events = get_video_events(student, module_key, limitId)
    #Codigo J. A. Gascon ESTO HAY QUE QUITARLO LUEGO
    
    #If there are new events, modify the lastKnownTracklog table to update/add the last known processed event
    if events:
        #Last known processed event of a student (student) for a certain course (course_key)           
        lastKnownProcessedEvent = events.order_by('time').reverse()[0]

        #Check if there is already a last known event registered in the database for a student in a certain course
        if lastKnownEventRegistered == 1:  
            #Update the last known processed event in the database (just update the id, the rest stays the same)
            lastKnownEvent.event_id = lastKnownProcessedEvent.id
            lastKnownEvent.save()
       
        else:  
            #Create the last kwnon processed event in the database
            LastKnownTrackingLog.objects.create(event_id=lastKnownProcessedEvent.id, username=lastKnownProcessedEvent.username, course_key=course_key, module_key=module_key, indicator=indicator)  

    else:
        events = None
    
    #print "EVENTS IN GETMODULE"
    #print events
    return events

 
def minutes_between(d1, d2):
    print 'tiempos'
    print d1
    print d2
    elapsed_time = d2 - d1
    return (elapsed_time.days * 86400 + elapsed_time.seconds)/60  

   
# Returns video consumption non-overlapped (%) and total (in seconds)
# for a certain student relative and absolute time watched for every 
# video in video_module_ids
def video_consumption(user_video_intervals, video_durations):
    # Non-overlapped video time
    stu_video_seen = []
    # Total video time seen
    all_video_time = []    
    for video in user_video_intervals:
        interval_sum = 0
        aux_start = video.disjointed_start
        aux_end = video.disjointed_end
        video_time = 0
        interval_start = video.interval_start
        interval_end = video.interval_end
        for start, end in zip(aux_start,aux_end):
            interval_sum += end - start
        for int_start, int_end in zip(interval_start, interval_end):
            video_time += int_end - int_start            
        stu_video_seen.append(interval_sum)
        all_video_time.append(video_time)
    if sum(stu_video_seen) <= 0:
        return None, None
        '''
        no_video_viewed = [0 for i in user_video_intervals]
        return no_video_viewed, no_video_viewed
        '''
    video_percentages = map(truediv, stu_video_seen, video_durations)
    video_percentages = [int(round(val*100,0)) for val in video_percentages]
    # Artificially ensures  percentages do not surpass 100%, which
    # could happen slightly from the 1s adjustment in id_to_length function
    for i in range(0,len(video_percentages)):
        if video_percentages[i] > 100:
            video_percentages[i] = 100
  
    return video_percentages, all_video_time

# Determines the histogram information for a certain video given the 
# intervals a certain user has viewed and the video_duration itself
def histogram_from_intervals(interval_start, interval_end, video_duration):
  
    hist_xaxis = list(interval_start + interval_end) # merge the two lists
    hist_xaxis.append(0) # assure xaxis stems from the beginning (video_pos = 0 secs)
    hist_xaxis.append(int(video_duration)) # assure xaxis covers up to video length
    hist_xaxis = list(set(hist_xaxis)) # to remove duplicates
    hist_xaxis.sort() # abscissa values for histogram    
    midpoints = []
    for index in range(0, len(hist_xaxis)-1):
        midpoints.append((hist_xaxis[index] + hist_xaxis[index+1])/float(2))

    # ordinate values for histogram
    hist_yaxis = get_hist_height(interval_start, interval_end, midpoints)# set histogram height
    return hist_xaxis, hist_yaxis

# Determine video histogram height
def get_hist_height(interval_start, interval_end, points):
  
    open_intervals = 0 # number of open intervals
    close_intervals = 0 # number of close intervals
    hist_yaxis = []
    for point in points:
        for start in interval_start:
            if point >= start:
                open_intervals += 1
            else:
                break
        for end in interval_end:
            if point > end:
                close_intervals += 1                
        hist_yaxis.append(open_intervals - close_intervals)
        open_intervals = 0
        close_intervals = 0
        
    return hist_yaxis

    
# Returns an ordered list of intervals for start and ending points        
def sort_intervals(interval_start, interval_end):
  
    interval_start, interval_end = zip(*sorted(zip(interval_start, interval_end)))
    interval_start = list(interval_start)
    interval_end = list(interval_end)
    
    return interval_start, interval_end


# Returns an ordered list of intervals and ids for start and ending points        
def sort_intervals_ids(interval_start, interval_end, ids):
  
    interval_start, interval_end, ids = zip(*sorted(zip(interval_start, interval_end,ids)))
    interval_start = list(interval_start)
    interval_end = list(interval_end)
    ids = list(ids)
    
    return interval_start, interval_end, ids


# Returns daily time devoted to the activity described by the arguments
# Intervals start and end are both relative to video position
# Times start and end are both lists of Django's DateTimeField.
def get_daily_time(interval_start, interval_end, time_start, time_end):
    # Sort all list by time_start order
    [time_start, time_end, interval_start, interval_end] = zip(*sorted(zip(time_start, time_end, interval_start, interval_end)))
    interval_start = list(interval_start)
    interval_end = list(interval_end)
    time_start = list(time_start)
    time_end = list(time_end)
    
    days = [time_start[0].date()]
    daily_time = [0]
    i = 0 
    while i < len(time_start):
        if days[-1] == time_start[i].date(): # another interval to add to the same day
            if time_end[i].date() == time_start[i].date(): # the interval belongs to a single day
                daily_time[-1] += interval_end[i] - interval_start[i]
            else: # interval extrems lay on different days. E.g. starting on day X at 23:50 and ending the next day at 0:10. 
                daily_time[-1] += 24*60*60 - time_start[i].hour*60*60 - time_start[i].minute*60 - time_start[i].second
                days.append(time_end[i].date())
                daily_time.append(time_end[i].hour*60*60 + time_end[i].minute*60 + time_end[i].second)
        else:
            days.append(time_start[i].date())
            daily_time.append(0)
            if time_end[i].date() == time_start[i].date(): # the interval belongs to a single day
                daily_time[-1] += interval_end[i] - interval_start[i]
            else: # interval extrems lay on different days. E.g. starting on day X at 23:50 and ending the next day at 0:10.
                daily_time[-1] += 24*60*60 - time_start[i].hour*60*60 - time_start[i].minute*60 - time_start[i].second
                days.append(time_end[i].date())
                daily_time.append(time_end[i].hour*60*60 + time_end[i].minute*60 + time_end[i].second)            
        i += 1
    # Convert days from datetime.date to str in format YYYY-MM-DD
    # Currently this conversion takes place outside this function. Therefore, commented out.
    """
    days_yyyy_mm_dd = []
    for day in days:
        days_yyyy_mm_dd.append(day.isoformat())
    """
    return  days, daily_time


#TODO Does it make sense to change the resolution to minutes?
# Returns daily time spent on a video for a the user
def daily_time_on_video(interval_start, interval_end, vid_start_time, vid_end_time):

    # We could check on either vid_start_time or vid_end_time for unwatched video
    if len(vid_start_time) > 0:
        video_days, video_daily_time = get_daily_time(interval_start, interval_end, vid_start_time, vid_end_time)
    else:
        video_days, video_daily_time = None, None
    
    return video_days, video_daily_time

    
# Computes the time (in seconds) a student has dedicated
# to videos (any of them) on a daily basis
#TODO Does it make sense to change the resolution to minutes?
# Receives as argument a list of UserVideoIntervals
def daily_time_on_videos(user_video_intervals):
    accum_days = []
    accum_daily_time = []
    for video in user_video_intervals:
        interval_start = video.interval_start
        interval_end = video.interval_end
        vid_start_time = video.vid_start_time
        vid_end_time = video.vid_end_time
        days, daily_time = daily_time_on_video(interval_start, interval_end, vid_start_time, vid_end_time)
        if days is not None and daily_time is not None:
            accum_days = accum_days + days
            accum_daily_time = accum_daily_time + daily_time
    if len(accum_days) <= 0:
        return None, None
    days = list(set(accum_days)) # to remove duplicates
    days.sort()
    daily_time = []
    for i in range(0,len(days)):
        daily_time.append(0)
        while True:
            try:
                daily_time[i] += accum_daily_time[accum_days.index(days[i])]
                accum_daily_time.pop(accum_days.index(days[i]))
                accum_days.remove(days[i])
            except ValueError:
                break

    return days, daily_time
   

# Given a video event from the track_trackinglogs MySQL table returns currentTime depending on event_type
# currentTime is the position in video the event refers to
# For play_video, pause_video and speed_change_video events it refers to where video was played, paused or the speed was changed.
# For seek_video event it's actually new_time and old_time where the user moved to and from
def get_current_time(video_event):
  
    current_time = []
    
    if video_event.event_type == 'play_video' or video_event.event_type == 'pause_video':
        current_time = [round(eval(video_event.event)['currentTime'])]
    elif video_event.event_type == 'speed_change_video':
        current_time = [round(eval(video_event.event)['current_time'])]
    elif video_event.event_type == 'seek_video':
        current_time = [round(eval(video_event.event)['old_time']), round(eval(video_event.event)['new_time'])]
    
    return current_time


# Receives as argument [[CTs for play], [CTs for pause], [CTs for speed changes], [old_time list], [new_time list]]    
# Adapts events_times as returned from get_video_events(**kwargs) to Google Charts' scatter chart
# CT: current time
def video_events_to_scatter_chart(events_times):
    scatter_array = [['Position (s)','Play', 'Pause', 'Change speed', 'Seek from', 'Seek to']]
    i = 0
    for event_times in events_times:
        i += 1
        if len(event_times) <= 0:
            scatter_array.append([None, None, None, None, None, None])
            scatter_array[-1][i] = i
        else:
            for event_time in event_times:
                scatter_array.append([event_time, None, None, None, None, None])
                scatter_array[-1][i] = i
    
    return json.dumps(scatter_array)


# Convert a time in format HH:MM:SS to seconds
def hhmmss_to_secs(hhmmss):
    if re.match('[0-9]{2}(:[0-5][0-9]){2}', hhmmss) is None:
        return 0
    else:
        split = hhmmss.split(':')
        hours = int(split[0])
        minutes = int(split[1])
        seconds = int(split[2])
        return hours*60*60+minutes*60+seconds
    
# Returns time spent on every problem in problem_ids for a certain student
def problem_consumption(user_time_on_problems):
  
    time_x_problem = []
    #print 'user_time_on_problems'
    for problem in user_time_on_problems:
        time_x_problem.append(problem.problem_time)
    if sum(time_x_problem) <= 0:
        time_x_problem = None
        
    return time_x_problem

    
# Computes the time (in seconds) a student has dedicated
# to problems (any of them) on a daily basis
#TODO Does it make sense to change the resolution to minutes?
def time_on_problems(user_time_on_problems):

    accum_days = []
    accum_daily_time = []
    for user_time_on_problem in user_time_on_problems:
        days = user_time_on_problem.days
        daily_time = user_time_on_problem.daily_time
        if len(days) > 0:
            accum_days = accum_days + days
            accum_daily_time = accum_daily_time + daily_time
    if len(accum_days) <= 0:
        return None, None        
        
    days = list(set(accum_days)) # to remove duplicates
    days.sort()
    daily_time = []
    for i in range(0,len(days)):
        daily_time.append(0)
        while True:
            try:
                daily_time[i] += accum_daily_time[accum_days.index(days[i])]
                accum_daily_time.pop(accum_days.index(days[i]))
                accum_days.remove(days[i])
            except ValueError:
                break
    
    return days, daily_time

# Return two lists for video and problem descriptors respectively in the course
def videos_problems_in(course_descriptor):

    MODULES_TO_FIND = [u'video', u'problem']
    # Lists for video and problem descriptors
    videos_in = []
    problems_in = []
    video_problem_list = [videos_in, problems_in]
    
    for chapter in course_descriptor.get_children():
        if chapter.location.category in MODULES_TO_FIND:
            video_problem_list[MODULES_TO_FIND.index(chapter.location.category)].append(chapter)
        else:
            for sequential_or_videosequence in chapter.get_children():
                if sequential_or_videosequence.location.category in MODULES_TO_FIND:
                    video_problem_list[MODULES_TO_FIND.index(sequential_or_videosequence.location.category)].append(sequential_or_videosequence)
                else:
                    for vertical_or_problemset in sequential_or_videosequence.get_children():
                        if vertical_or_problemset.location.category in MODULES_TO_FIND:
                            video_problem_list[MODULES_TO_FIND.index(vertical_or_problemset.location.category)].append(vertical_or_problemset)
                            #print 'VIDEO_PROBLEM'
                            #print video_problem_list[MODULES_TO_FIND.index(sequential_or_videosequence.location.category)]
                        else:
                            for content in vertical_or_problemset.get_children():
                                if content.location.category in MODULES_TO_FIND:
                                    video_problem_list[MODULES_TO_FIND.index(content.location.category)].append(content)
                                   
    
    #print 'LISTA ENTERA DE VIDEOS'
    #print video_problem_list[0]
    #print 'CONTENT doc'
    #print video_problem_list[0].__name__
    return video_problem_list


# Computes the aggregated time (in seconds) all students in a course (the whole class)
# have dedicated to a module type on a daily basis
#TODO Does it make sense to change the resolution to minutes?
def class_time_on(accum_days, accum_daily_time):

    if len(accum_days) <= 0:
        return None, None

    days = list(set(accum_days)) # to remove duplicates
    days.sort()
    daily_time = []
    for i in range(0,len(days)):
        daily_time.append(0)
        while True:
            try:
                daily_time[i] += accum_daily_time[accum_days.index(days[i])]
                accum_daily_time.pop(accum_days.index(days[i]))
                accum_days.remove(days[i])
            except ValueError:
                break        

    return days, daily_time    
    
# Returns an array in JSON format ready to use for the arrayToDataTable method of Google Charts
def ready_for_arraytodatatable(column_headers, *columns):

    if columns[-1] is None or columns[-1] == []:
        return json.dumps(None)
        
    array_to_data_table = []
    array_to_data_table.append(column_headers)
    if len(columns) > 0:
        for i in range(0, len(columns[0])):
            row = []
            for column in columns:
                row.append(column[i])
            array_to_data_table.append(row)
            
    return json.dumps(array_to_data_table)


# Once we have daily time spent on video and problems, we need to put these informations together
# so that they can be jointly represented in a column chart.
# Thought for ColumnChart of Google Charts.
# Output convenient for Google Charts' arrayToDataTable()  [['day', video_time, problem_time], []]
def join_video_problem_time(video_days, video_daily_time, problem_days, problem_daily_time):

    days = list(set(video_days + problem_days)) # join days and remove duplicates
    # Check whether neither video watched nor problem tried
    if len(days) <= 0:
        return json.dumps(None)

    days.sort() # order the list
    # list of lists containing date, video time and problem time
    output_array = []
    for i in range(0, len(days)):
        output_array.append([days[i],0,0])
        try: # some video time that day
            auxiliar = video_daily_time[video_days.index(days[i])]
            output_array[i][1] = auxiliar
        except ValueError:
            pass
        try: # some problem time that day
            auxiliar = problem_daily_time[problem_days.index(days[i])]
            output_array[i][2] = auxiliar
        except ValueError:
            pass
    # Insert at the list start the column information
    output_array.insert(0, ['Day', 'Video time', 'Problem time'])
    
    return json.dumps(output_array)

    
def to_iterable_module_id(block_usage_locator):
    #print 'block_usage_locator'
    #print block_usage_locator
    #print block_usage_locator.branch
    #print block_usage_locator.version_guid
    iterable_module_id = []
    iterable_module_id.append(block_usage_locator.org)
    iterable_module_id.append(block_usage_locator.course)
    #iterable_module_id.append(block_usage_locator.run)
    iterable_module_id.append(block_usage_locator.branch)
    iterable_module_id.append(block_usage_locator.version_guid)
    iterable_module_id.append(block_usage_locator.block_type)
    iterable_module_id.append(block_usage_locator.block_id)    
    
    return iterable_module_id
    

# Converts a list of python datetime objets to a list
# of their equivalent strings
def datelist_to_isoformat(date_list):
  
    return [date.isoformat() for date in date_list]


def determine_repetitions_vticks(maxRepetitions):

    # parameter to set the number of gridlines apart from 0.
    # Number of vertical ticks necessary.
    NUM_GRIDLINES = 4 
    if maxRepetitions <= 4:
        vticks = range(0, maxRepetitions+1)
    else:
        # smallest number greater than maxRepetitions multiple of NUM_GRIDLINES
        smallest_x_greater = (maxRepetitions/NUM_GRIDLINES + 1) * NUM_GRIDLINES
        vticks = range(0, smallest_x_greater+1, NUM_GRIDLINES)
  
    return vticks
