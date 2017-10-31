#!/usr/bin/env python
# -*- coding: utf-8 -*-
# chcp 65001

"""
Idea was originally from:
  Phil Adams http://philadams.net
  http://github.com/philadams/habitica

"""


from bisect import bisect
import json
import logging
import os.path
import pickle
from time import sleep
from webbrowser import open_new_tab

from docopt import docopt

import api

from pprint import pprint

try:
    import ConfigParser as configparser
except:
    import configparser


VERSION = 'habitica version 0.0.16'
TASK_VALUE_BASE = 0.9747  # http://habitica.wikia.com/wiki/Task_Value
HABITICA_REQUEST_WAIT_TIME = 0.5  # time to pause between concurrent requests
HABITICA_TASKS_PAGE = '/#/tasks'
# https://trello.com/c/4C8w1z5h/17-task-difficulty-settings-v2-priority-multiplier
PRIORITY = {'easy': 1,
            'medium': 1.5,
            'hard': 2}
AUTH_CONF = 'auth.cfg'
CACHE_CONF = 'cache.cfg'

SECTION_CACHE_QUEST = 'Quest'
checklists_on = False

DEFAULT_PARTY = 'Not currently in a party'
DEFAULT_QUEST = 'Not currently on a quest'
DEFAULT_PET = 'No pet currently'
DEFAULT_MOUNT = 'Not currently mounted'


def load_auth(configfile):
    """Get authentication data from the AUTH_CONF file."""

    logging.debug('Loading habitica auth data from %s' % configfile)

    try:
        cf = open(configfile)
    except IOError:
        logging.error("Unable to find '%s'." % configfile)
        exit(1)

    config = configparser.ConfigParser({'checklists': False})
    config.readfp(cf)

    cf.close()

    # Get data from config
    rv = {}
    try:
        rv = {'url': config.get('Habitica', 'url'),
              'checklists': config.get('Habitica', 'checklists'),
              'x-api-user': config.get('Habitica', 'login'),
              'x-api-key': config.get('Habitica', 'password')}

    except configparser.NoSectionError:
        logging.error("No 'Habitica' section in '%s'" % configfile)
        exit(1)

    except configparser.NoOptionError as e:
        logging.error("Missing option in auth file '%s': %s"
                      % (configfile, e.message))
        exit(1)

    # Return auth data as a dictionary
    return rv


def load_cache(configfile):
    logging.debug('Loading cached config data (%s)...' % configfile)

    defaults = {'quest_key': '',
                'quest_s': 'Not currently on a quest'}

    cache = configparser.ConfigParser(defaults)
    cache.read(configfile)

    if not cache.has_section(SECTION_CACHE_QUEST):
        cache.add_section(SECTION_CACHE_QUEST)

    return cache


def update_quest_cache(configfile, **kwargs):
    logging.debug('Updating (and caching) config data (%s)...' % configfile)

    cache = load_cache(configfile)

    for key, val in kwargs.items():
        cache.set(SECTION_CACHE_QUEST, key, val)

    with open(configfile, 'wb') as f:
        cache.write(f)

    cache.read(configfile)

    return cache


def get_task_ids(tids, unique_and_sort=True):
    """
    handle task-id formats such as:
        habitica todos done 3
        habitica todos done 1,2,3
        habitica todos done 2 3
        habitica todos done 1-3,4 8
    tids is a seq like (last example above) ('1-3,4' '8')
    """
    logging.debug('raw task ids: %s' % tids)
    task_ids = []
    for raw_arg in tids:
        for bit in raw_arg.split(','):
            if '-' in bit:
                start, stop = [int(e) for e in bit.split('-')]
                task_ids.extend(range(start, stop + 1))
            else:
                task_ids.append(int(bit))
    if unique_and_sort:
        return [e - 1 for e in set(task_ids)]
    else:
        return [e - 1 for e in task_ids]


def updated_task_list(tasks, tids):
    for tid in sorted(tids, reverse=True):
        del(tasks[tid])
    return tasks


def cl_done_count(task):
    items = task['checklist']
    count = 0
    for li in items:
        if li['completed'] == True:
            count = count + 1
    return count


def cl_item_count(task):
    if 'checklist' in task:
        return len(task['checklist'])
    else:
        return 0


def print_task_list(tasks, note_first=False):
    for i, task in enumerate(tasks):
        completed = 'x' if task['completed'] else ' '
        if note_first:
            task_line = '[{}] {} <{}> {}'.format(completed,
                                                 i+1,
                                                 task['notes'][:50],
                                                 task['text'])
        else:
            task_line = '[%s] %s %s <%s>' % (completed,
                                        i + 1,
                                        task['text'],
                                        task['notes'])
        checklist_available = cl_item_count(task) > 0
        if checklist_available:
            task_line += ' (%s/%s)' % (str(cl_done_count(task)),
                                       str(cl_item_count(task)))
        print(task_line)
        if checklists_on and checklist_available:
            for c, check in enumerate(task['checklist']):
                completed = 'x' if check['completed'] else ' '
                print('    [%s] %s' % (completed,
                                       check['text']))


def qualitative_task_score_from_value(value):
    # task value/score info: http://habitica.wikia.com/wiki/Task_Value
    scores = ['*', '**', '***', '****', '*****', '******', '*******']
    breakpoints = [-20, -10, -1, 1, 5, 10]
    return scores[bisect(breakpoints, value)]


def set_checklists_status(auth, args):
    """Set display_checklist status, toggling from cli flag"""
    global checklists_on

    if auth['checklists'] == "true":
        checklists_on = True
    else:
        checklists_on = False

    # reverse the config setting if specified by the CLI option
    if args['--checklists']:
        checklists_on = not checklists_on

    return


class Challenge:
    def __init__(self, _id, _name, _short_name):
        self.id = _id
        self.name = _name
        self.short_name = _short_name

    def print(self):
        print('Challenge: {}\n> ID: [{}]\n> Name: [{}]'.format(self.short_name, self.id, self.name))


def cli():
    """Habitica command-line interface.

    Usage: habitica [--version] [--help]
                    <command> [<args>...] [--difficulty=<d>]
                    [--verbose | --debug] [--checklists]

    Options:
      -h --help         Show this screen
      --version         Show version
      --difficulty=<d>  (easy | medium | hard) [default: easy]
      --verbose         Show some logging information
      --debug           Some all logging information
      -c --checklists   Toggle displaying checklists on or off

    The habitica commands are:
      status                  Show HP, XP, GP, and more
      habits                  List habit tasks
      habits up <task-id>     Up (+) habit <task-id>
      habits down <task-id>   Down (-) habit <task-id>
      hb                      List habit with desc first and then title
      hb top <task-id>        Move habit to top of the list
      hb tob <task-id>        Move habit to bottom of the list
      hb <task-id> to <pos>   Move habit to a position
      dailies                 List daily tasks
      dailies done            Mark daily <task-id> complete
      dailies undo            Mark daily <task-id> incomplete
      dl                      List daily tasks with desc first and then title
      dl top <task-id>        Move dailys to top of the list
      dl tob <task-id>        Move dailys to bottom of the list
      dl <task-id> to <pos>   Move daily to a position
      todos                   List todo tasks
      todos done <task-id>    Mark one or more todo <task-id> completed
      todos add <task>        Add todo with description <task>
      todos delete <task-id>  Delete one or more todo <task-id>
      todos top <task-ids>    Move todos to top
      todos tob <task-ids>    Move todos to bottom
      todos <task-id> to <pos> Move todo to a position
      cs                      List challenges
      cs listbroken           list broken(closed) challenges
      cs clean                Remove tasks of broken challenges
      server                  Show status of Habitica service
      home                    Open tasks page in default browser

    For `habits up|down`, `dailies done|undo`, `todos done`, and `todos
    delete`, you can pass one or more <task-id> parameters, using either
    comma-separated lists or ranges or both. For example, `todos done
    1,3,6-9,11`.

    To show checklists with "todos" and "dailies" permanently, set
    'checklists' in your auth.cfg file to `checklists = true`.

    Try `chcp 65001` when has char error.
    """

    def get_challenges(use_cache=False):  # cache is for quick debugging
        cache_file = 'challenge_cache.pkl'
        if use_cache:
            with open(cache_file, 'rb') as pkl:
                challenges = pickle.load(pkl)
        else:
            challenges = [Challenge(c['_id'], c['name'], c['shortName']) for c in hbt.challenges.user()]
            with open(cache_file, 'wb') as pkl:
                pickle.dump(challenges, pkl)
        return challenges

    def in_challenges(cs_id, _cs):
        for c in _cs:
            if cs_id == c.id:
                return True
        return False

    def get_all_tasks(use_cache=False):  # cache is for quick debugging
        cache_file = 'tasks_cache.pkl'
        if use_cache:
            with open(cache_file, 'rb') as pkl:
                tasks = pickle.load(pkl)
        else:
            tasks = hbt.user.tasks(type='habits')
            tasks.extend(hbt.user.tasks(type='dailys'))
            tasks.extend([e for e in hbt.user.tasks(type='todos') if not e['completed']])
            with open(cache_file, 'wb') as pkl:
                pickle.dump(tasks, pkl)
        return tasks

    def print_broken_challenges(use_cache=False):
        ts = get_all_tasks()#use_cache)
        challenge_names = []
        task_names = []
        for t in ts:
            if 'broken' in t['challenge']:
                challenge_names.append((t['challenge']['broken'], t['challenge']['shortName']))
                task_names.append((t['type'], t['notes'], t['text']))
        if len(challenge_names) == 0:
            print('There are no broken challenges')
        else:
            print('Broken challenges (already closed while have some tasks belong to them):')
            for name in set(challenge_names):
                print('> {}: [{}]'.format(name[0], name[1]))
            print('Tasks that have broken challenges:')
            for name in task_names:
                print('> [{}]\t[{}]\t[{}]'.format(name[0], name[1], name[2]))

    def move(cmd_args, tasks, moveto, movefrom=None):
        # https://habitica.com/api/v3/tasks/:taskId/move/to/:position
        args_ids = cmd_args['<args>'][1:] if movefrom is None else [movefrom]
        tids = get_task_ids(args_ids, unique_and_sort=False)
        real_ids = [tasks[tid] for tid in tids]
        for real_id in real_ids:
            print('moving', real_id['text'], 'to',
                  'top' if moveto == '0' else 'bottom' if moveto == '-1' else moveto)
            hbt.user.tasks(_id=real_id['id'], _method='post', _moveto=moveto)
            sleep(HABITICA_REQUEST_WAIT_TIME)

    # set up args
    args = docopt(cli.__doc__, version=VERSION)

    # set up logging
    if args['--verbose']:
        logging.basicConfig(level=logging.INFO)
    if args['--debug']:
        logging.basicConfig(level=logging.DEBUG)

    logging.debug('Command line args: {%s}' %
                  ', '.join("'%s': '%s'" % (k, v) for k, v in args.items()))

    # Set up auth
    auth = load_auth(AUTH_CONF)

    # Prepare cache
    cache = load_cache(CACHE_CONF)

    # instantiate api service
    hbt = api.Habitica(auth=auth)

    # Flag checklists as on if true in the config
    set_checklists_status(auth, args)

    # GET server status
    if args['<command>'] == 'server':
        server = hbt.status()
        if server['status'] == 'up':
            print('Habitica server is up')
        else:
            print('Habitica server down... or your computer cannot connect')

    # open HABITICA_TASKS_PAGE
    elif args['<command>'] == 'home':
        home_url = '%s%s' % (auth['url'], HABITICA_TASKS_PAGE)
        print('Opening %s' % home_url)
        open_new_tab(home_url)

    # GET user
    elif args['<command>'] == 'status':

        # gather status info
        user = hbt.user()
        stats = user.get('stats', '')
        items = user.get('items', '')
        food_count = sum(items['food'].values())
        group = hbt.groups(type='party')
        party = DEFAULT_PARTY
        quest = DEFAULT_QUEST
        mount = DEFAULT_MOUNT

        # if in a party, grab party info
        if group:
            party_id = group[0]['id']
            party_title = group[0]['name']

            # if on a quest with the party, grab quest info
            quest_data = getattr(hbt.groups, party_id)()['quest']
            if quest_data and quest_data['active']:
                quest_key = quest_data['key']

                if cache.get(SECTION_CACHE_QUEST, 'quest_key') != quest_key:
                    # we're on a new quest, update quest key
                    logging.info('Updating quest information...')
                    content = hbt.content()
                    quest_type = ''
                    quest_max = '-1'
                    quest_title = content['quests'][quest_key]['text']

                    # if there's a content/quests/<quest_key/collect,
                    # then drill into .../collect/<whatever>/count and
                    # .../collect/<whatever>/text and get those values
                    if content.get('quests', {}).get(quest_key,
                                                     {}).get('collect'):
                        logging.debug("\tOn a collection type of quest")
                        qt = 'collect'
                        clct = content['quests'][quest_key][qt].values()[0]
                        quest_max = clct['count']
                    # else if it's a boss, then hit up
                    # content/quests/<quest_key>/boss/hp
                    elif content.get('quests', {}).get(quest_key,
                                                       {}).get('boss'):
                        logging.debug("\tOn a boss/hp type of quest")
                        qt = 'hp'
                        quest_max = content['quests'][quest_key]['boss'][qt]

                    # store repr of quest info from /content
                    cache = update_quest_cache(CACHE_CONF,
                                               quest_key=str(quest_key),
                                               quest_type=str(qt),
                                               quest_max=str(quest_max),
                                               quest_title=str(quest_title))

                # now we use /party and quest_type to figure out our progress!
                quest_type = cache.get(SECTION_CACHE_QUEST, 'quest_type')
                if quest_type == 'collect':
                    qp_tmp = quest_data['progress']['collect']
                    quest_progress = qp_tmp.values()[0]
                else:
                    quest_progress = quest_data['progress']['hp']

                quest = '%s/%s "%s"' % (
                        str(int(quest_progress)),
                        cache.get(SECTION_CACHE_QUEST, 'quest_max'),
                        cache.get(SECTION_CACHE_QUEST, 'quest_title'))

        # prepare and print status strings
        title = 'Level %d %s' % (stats['lvl'], stats['class'].capitalize())
        health = '%d/%d' % (stats['hp'], stats['maxHealth'])
        xp = '%d/%d' % (int(stats['exp']), stats['toNextLevel'])
        mana = '%d/%d' % (int(stats['mp']), stats['maxMP'])
        currentPet = items.get('currentPet', '')
        if not currentPet:
            currentPet = DEFAULT_PET
        pet = '%s (%d food items)' % (currentPet, food_count)
        mount = items.get('currentMount', '')
        if not mount:
            mount = DEFAULT_MOUNT
        summary_items = ('health', 'xp', 'mana', 'quest', 'pet', 'mount')
        len_ljust = max(map(len, summary_items)) + 1
        print('-' * len(title))
        print(title)
        print('-' * len(title))
        print('%s %s' % ('Health:'.rjust(len_ljust, ' '), health))
        print('%s %s' % ('XP:'.rjust(len_ljust, ' '), xp))
        print('%s %s' % ('Mana:'.rjust(len_ljust, ' '), mana))
        print('%s %s' % ('Pet:'.rjust(len_ljust, ' '), pet))
        print('%s %s' % ('Mount:'.rjust(len_ljust, ' '), mount))
        print('%s %s' % ('Party:'.rjust(len_ljust, ' '), party))
        print('%s %s' % ('Quest:'.rjust(len_ljust, ' '), quest))

    # GET/POST habits
    elif args['<command>'] == 'habits':
        habits = hbt.user.tasks(type='habits')
        if 'up' in args['<args>']:
            tids = get_task_ids(args['<args>'][1:])
            for tid in tids:
                tval = habits[tid]['value']
                hbt.user.tasks(_id=habits[tid]['id'],
                               _direction='up', _method='post')
                print('incremented task \'%s\''
                      % habits[tid]['text'].encode('utf8'))
                habits[tid]['value'] = tval + (TASK_VALUE_BASE ** tval)
                sleep(HABITICA_REQUEST_WAIT_TIME)
        elif 'down' in args['<args>']:
            tids = get_task_ids(args['<args>'][1:])
            for tid in tids:
                tval = habits[tid]['value']
                hbt.user.tasks(_id=habits[tid]['id'],
                               _direction='down', _method='post')
                print('decremented task \'%s\''
                      % habits[tid]['text'].encode('utf8'))
                habits[tid]['value'] = tval - (TASK_VALUE_BASE ** tval)
                sleep(HABITICA_REQUEST_WAIT_TIME)
        for i, task in enumerate(habits):
            score = qualitative_task_score_from_value(task['value'])
            print('[%s] %s %s' % (score, i + 1, task['text'].encode('utf8')))

    # habits with notes
    elif args['<command>'] == 'hb':
        cache_file = 'habitica_cache.pkl'
        if False:  # use cache ?
            with open(cache_file, 'rb') as pkl:
                habits = pickle.load(pkl)
        else:
            habits = hbt.user.tasks(type='habits')
            with open(cache_file, 'wb') as pkl:
                pickle.dump(habits, pkl)

        if 'top' in args['<args>']:
            move(args, habits, moveto='0')
        elif 'tob' in args['<args>']:
            move(args, habits, moveto='-1')
        elif 'to' in args['<args>']:
            move(args, habits, movefrom=args['<args>'][0], moveto=args['<args>'][2])
        else:
            for i, habit in enumerate(habits):
                print('[{}] [{}] {}'.format(i+1, habit['notes'], habit['text']))

    # GET/PUT tasks:daily
    elif args['<command>'] == 'dailies':
        dailies = hbt.user.tasks(type='dailys')
        if 'done' in args['<args>']:
            tids = get_task_ids(args['<args>'][1:])
            for tid in tids:
                hbt.user.tasks(_id=dailies[tid]['id'],
                               _direction='up', _method='post')
                print('marked daily \'%s\' completed'
                      % dailies[tid]['text'].encode('utf8'))
                dailies[tid]['completed'] = True
                sleep(HABITICA_REQUEST_WAIT_TIME)
        elif 'undo' in args['<args>']:
            tids = get_task_ids(args['<args>'][1:])
            for tid in tids:
                hbt.user.tasks(_id=dailies[tid]['id'],
                               _method='put', completed=False)
                print('marked daily \'%s\' incomplete'
                      % dailies[tid]['text'].encode('utf8'))
                dailies[tid]['completed'] = False
                sleep(HABITICA_REQUEST_WAIT_TIME)
        print_task_list(dailies)

    # dailies with notes
    elif args['<command>'] == 'dl':
        dailies = hbt.user.tasks(type='dailys')
        if 'top' in args['<args>']:
            move(args, dailies, moveto='0')
        elif 'tob' in args['<args>']:
            move(args, dailies, moveto='-1')
        elif 'to' in args['<args>']:
            move(args, dailies, movefrom=args['<args>'][0], moveto=args['<args>'][2])
        else:
            print_task_list(dailies, note_first=True)

    # GET tasks:todo
    elif args['<command>'] == 'todos':
        todos = [e for e in hbt.user.tasks(type='todos')
                 if not e['completed']]
        if 'done' in args['<args>']:
            tids = get_task_ids(args['<args>'][1:])
            for tid in tids:
                hbt.user.tasks(_id=todos[tid]['id'],
                               _direction='up', _method='post')
                print('marked todo \'%s\' complete'
                      % todos[tid]['text'].encode('utf8'))
                sleep(HABITICA_REQUEST_WAIT_TIME)
            todos = updated_task_list(todos, tids)
        elif 'add' in args['<args>']:
            ttext = ' '.join(args['<args>'][1:])
            hbt.user.tasks(type='todo',
                           text=ttext,
                           priority=PRIORITY[args['--difficulty']],
                           _method='post')
            todos.insert(0, {'completed': False, 'text': ttext})
            print('added new todo \'%s\'' % ttext)
        elif 'delete' in args['<args>']:
            tids = get_task_ids(args['<args>'][1:])
            for tid in tids:
                # https://habitica.com/api/v3/tasks/:taskId
                # e.g. curl -X "DELETE" https://habitica.com/api/v3/tasks/3d5d324d-a042-4d5f-872e-0553e228553e
                hbt.user.tasks(_id=todos[tid]['id'],
                               _method='delete')
                print('deleted todo \'%s\''
                      % todos[tid]['text'])
                sleep(HABITICA_REQUEST_WAIT_TIME)
            todos = updated_task_list(todos, tids)

        if 'top' in args['<args>']:
            move(args, todos, moveto='0')
        elif 'tob' in args['<args>']:
            move(args, todos, moveto='-1')
        elif 'to' in args['<args>']:
            move(args, todos, movefrom=args['<args>'][0], moveto=args['<args>'][2])
        else:
            print_task_list(todos)

    # GET challenges
    elif args['<command>'] == 'cs':
        if 'listbroken' in args['<args>']:
            print_broken_challenges() #use_cache=True)

        # https://habitica.com/api/v3/tasks/unlink-one/757c831c-b4c6-4697-b6f9-ec30ee41e8e0?keep=remove
        elif 'clean' in args['<args>']:
            ts = get_all_tasks()
            broken_challenge_ids = set([t['challenge']['id'] for t in ts
                                        if 'broken' in t['challenge']
                                        and t['challenge']['broken'] == 'CHALLENGE_CLOSED'])
            for cid in broken_challenge_ids:
                hbt.tasks.unlink_all(_id=cid, _method='post', keep='remove-all')
            print_broken_challenges()

        else:
            cs = get_challenges()
            for c in cs:
                c.print()


if __name__ == '__main__':
    cli()
