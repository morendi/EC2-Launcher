#!/usr/bin/env python2

import ConfigParser
import os

c = ConfigParser.ConfigParser()
c.readfp(open('/etc/ec2config'))

# Configuration for our environment
prod_access_key = c.get("prod", "access_key")
test_access_key = c.get("test", "access_key")

prod_secret_key = c.get("prod", "secret_key")
test_secret_key = c.get("test", "secret_key")

key_path = c.get("ec2_launcher", "key_path")

import boto
import sys, os
import re
import io
import subprocess
import urwid

# Use for testing, since we can't print debugging statements
log = open("log", "w")

# The possible commands that can be invoked from the command line.  If one of
# these commands is not selected, then "list" will be chosen by default.
possible_commands = [ 'list', 'ssh', 'scp' ]
def usage():
    print """ec2 <prod|test> <command> [options]
    
  COMMANDS
    list 
        - list the servers in the selected environment
    ssh [<user>@]<server>
        - log into the selected server
    scp [<user>@]<server>:<remote path> <local path>
        - copy a file from the server to <local path>
    scp <local path> [<user>@]<server>:<remote path>
        - copy a file from <local path> to the server

  OPTIONS
    sort
        - defines the way instances are sorted in the list.  values are:
            instance-id
            name
            security-group
            type
            public-dns
            private-dns
            ip-address
            status
            launch-time
          all sort values can be prefixed by a + or - to indicate 
          forward/reverse sorting.
    
"""

# Handle the command line
def handle_args(argv):
    command = None
    if len(argv) == 1 or argv[1] == '--help' or argv[1] == '-h':
        usage()
        return 1

    if argv[1] not in [ 'prod', 'test' ]:
        print("Error: first argument must be 'prod' or 'test'")
        return 1

    if len(argv) < 3:
        command = 'list'
    elif argv[2] not in possible_commands:
        print("Error: second argument must be a valid command: %s" % \
                ", ".join(possible_commands))
        return 1

    environment = argv[1]
    if not command:
        command = argv[2] 

    # Parse any options out of the command line to pass onto the cmd_ functions
    options = {
            'sort' : 'launch-time'
            }

    longopt_regex = re.compile("^--(.*?)=(.*)$")
    shortopt_regex = re.compile("^-(.*)$")

    for cmd in argv[3:]:
        m = longopt_regex.match(cmd)
        n = shortopt_regex.match(cmd)
        if m:
            opt = m.group(1)
            val = m.group(2)

            options[opt] = val

        elif n:
            opt = n.group(1)
            val = True

            options[opt] = val

    return (options, command, environment)

def start_ec2(environment):
    if environment == 'prod':
        access_key = prod_access_key
        secret_key = prod_secret_key
    else:
        access_key = test_access_key
        secret_key = test_secret_key

    return boto.connect_ec2(access_key, secret_key)
    
def get_instances(ec2):
    # Set up a map to hold all our instances with relevant data
    instances = dict()
    for reservation in ec2.get_all_instances():
        instance = reservation.instances[0]
        instances[instance.id] = instance
    
    out = {}
    for id in instances.keys():
        instance = instances[id]
        out[instance.id] = { \
                    'instance-id': instance.id \
                ,   'name': instance.tags['Name'] \
                        if 'Name' in instance.tags.keys() else '' \
                ,   'type': instance.instance_type \
                ,   'server-type': instance.key_name \
                ,   'public-dns': instance.dns_name \
                ,   'private-dns': instance.private_dns_name \
                ,   'ip-address': instance.ip_address \
                        if instance.ip_address != '' \
                        else instance.private_ip_address \
                ,   'status': instance.state \
                ,   'launch-time': instance.launch_time \
            }

    return out

####################
# GUI Stuff
####################
class status_bar(urwid.Edit):

    def __init__(self):
        urwid.Edit.__init__(self, '', align='left')
        self.clear_mode()

    def get_text_value(self):
        text = self.get_text()[0]
        if self.mode == 'ssh' or (self.mode[:3] == 'scp' and self.stage == 1):
            return text[6:]     # Strip "user: " off the front
        elif self.mode[:3] == 'scp':
            if self.stage == 2:
                return text[12:]
            elif self.stage == 3:
                return text[13:]
        elif self.mode == 'search':
            return text[1:]

    def get_text_attributes(self):
        return self.get_text()[1]

    def set_mode(self, mode):
        self.mode = mode
        if self.mode[:3] == 'scp':
            self.stage = 1

        if      self.mode == 'scp_up' or \
                self.mode == 'scp_down' or \
                self.mode == 'ssh':
            self.set_caption("user: ")
        elif self.mode == 'search':
            self.set_caption("/")

    def advance_stage(self):
        if self.mode[:3] == 'scp':
            self.stage += 1
            self.set_edit_text("")

            if self.stage == 2:
                self.set_caption("local file: ")
            elif self.stage == 3:
                self.set_caption("remote file: ")
            elif self.stage == 4:
                self.clear_mode()
        elif self.mode == 'ssh':
            self.clear_mode()

    def clear_mode(self):
        self.mode = ""
        self.stage = 0
        self.set_caption("")
        self.set_edit_text("")

class ec2_launcher(urwid.Frame):
    
    palette = [
            ('banner',                  'black', 'light gray', 'standout,underline'),
            ('header',                  'black', 'dark green', 'standout'),
            ('instance_row',            'white', 'black', 'standout'),
            ('instance_row_focused',    'white', 'light blue', 'standout,bold'),
            ('bg',                      'white', 'black'),
            ('command',                 'white', 'black', 'standout'),
            ('command_focused',         'black', 'light gray', 'standout'),
        ]

    instance_line_regex = re.compile( \
    '^(i-[0-9a-f]{8}):\s+(.*?)\s+(.*?)\s+(.*?)\s+(.*?)\s+(.*?)\s+(.*?)\s+(.*)$')

    def __init__(self, environment, instances):

        # Build a list of widgets using data
        list = []
        for item in instances:
            row_text = "%s: %25s %30s %15s %45s %45s %15s %15s %30s" % \
                        ( item['instance-id'] \
                        , item['name'] \
                        , item['server-type'] \
                        , item['type'] \
                        , item['public-dns'] \
                        , item['private-dns'] \
                        , item['ip-address'] \
                        , item['status'] \
                        , item['launch-time'])
            t = urwid.Text(row_text, align='left', wrap='clip')
            list.append(urwid.AttrMap(t, 'instance_row', 'instance_row_focused'))
        self.listwalker = urwid.SimpleListWalker(list)

        # These will be returned from this class's call to main().  The
        # command and data specify what the program is to do after displaying
        # the gui
        self.command = None
        self.data = None

        # Indicates whether we are focused on the command bar at the footer
        self.footer_input = False

        # Data we obtain from the control widget
        self.user = "root"
        self.local_file = ""
        self.remote_file = ""

        # Keep track of whether 'g' has been pressed.  Used for the command
        # 'gg', to return to the top of the list
        self.one_g = False

        # Keep track of search term
        self.search_phrase = ""
        self.searching = False

        # Dictionary of instances 
        self.instances = instances

        # GUI stuff
        self.listbox = urwid.ListBox(self.listwalker)
        self.header_txt = urwid.Text(('banner', "Koofers EC2 Launcher v%s (%s)"\
                % ("0.01", environment)), align='center')
        self.footer_txt = status_bar()


        self.content = urwid.AttrMap(self.listbox, 'bg')
        self.header = urwid.AttrMap(self.header_txt, 'header')
        self.footer = urwid.AttrMap(self.footer_txt, 'command', 'command_focused')
        urwid.Frame.__init__(self, self.content, self.header, self.footer)

    def main(self):
        self.loop = urwid.MainLoop(self, self.palette, \
                unhandled_input=self.input_handler)
        self.loop.run()

        return (self.command, self.data)

    def action_refresh(self):
        self.command = "refresh"
        raise urwid.ExitMainLoop()

    def action_ssh(self, line, user):
        self.command = "ssh"
        m = self.instance_line_regex.match(line)
        if m:
            instance_id = m.group(1)
            self.data = { \
                    'instance-id': instance_id, \
                    'user': user \
                }

        raise urwid.ExitMainLoop()

    def action_scp(self, line, direction, user, local_file, remote_file):
        self.command = "scp"
        m = self.instance_line_regex.match(line)
        if m:
            instance_id = m.group(1)
            self.data = { \
                    'instance-id': instance_id, \
                    'user': user, \
                    'direction': direction, \
                    'local_file': local_file, \
                    'remote_file': remote_file, \
                }

        raise urwid.ExitMainLoop()

    def search(self, search_str):
        focus = self.listwalker.get_focus()

        found = False
        self.just_searched = False
        while True:
            lastfocus = self.listwalker.get_focus()[1]
            elem = self.listwalker.get_focus()[0]
            text = elem.base_widget.get_text()[0]
            m = re.search("("+search_str+")", text[self.search_end:], \
                    re.IGNORECASE)
            if m:
                found = True
                self.search_end = m.end(1)
                break

            self.listwalker.set_focus(\
                    self.listwalker.get_focus()[1] + 1)
            self.search_end = 0
            if lastfocus == self.listwalker.get_focus()[1]:
                break

        self.footer_input = False
        self.set_focus('body')

        if found:
            self.just_searched = True
            self.footer_txt.set_caption("")
            self.footer_txt.set_edit_text("")
        else: 
            self.footer_txt.clear_mode()
            self.footer_txt.set_caption("No results found.")
            self.listwalker.set_focus(focus[1])


    def input_handler(self, input):
        focus = self.listwalker.get_focus()

        # Scroll down
        if input == 'j' or input == 'down':
            self.listwalker.set_focus(focus[1] + 1)

        # Scroll up
        elif input == 'k' or input == 'up':
            if focus[1] == 0:
                return
            self.listwalker.set_focus(focus[1] - 1)

        # Go to bottom of list
        elif input == 'G':
            self.listwalker.set_focus(len(self.instances) - 1)

        # Go to top of list
        elif input == 'g':
            if self.one_g:
                self.one_g = False
                self.listwalker.set_focus(0)
            else:
                self.one_g = True

        # Refresh list
        elif input == 'r' or input == 'R':
            self.action_refresh()

        # do an action
        elif input == 'enter' or input == 's':
            elem = focus[0]
            text = elem.base_widget.get_text()[0]
            if self.footer_input:
                if self.footer_txt.mode == 'ssh':
                    self.user = self.footer_txt.get_text_value()
                    self.footer_input = False
                    self.footer_txt.clear_mode()
                    self.set_focus('body')
                    self.action_ssh(text, self.user)
                elif self.footer_txt.mode[:3] == 'scp':
                    if self.footer_txt.stage == 1:
                        self.user = self.footer_txt.get_text_value()
                    elif self.footer_txt.stage == 2:
                        self.local_file = self.footer_txt.get_text_value()
                    elif self.footer_txt.stage == 3:
                        self.remote_file = self.footer_txt.get_text_value()
                        self.set_focus('body')
                        self.action_scp(text, \
                                self.footer_txt.mode[4:], \
                                self.user, \
                                self.local_file, \
                                self.remote_file )
                        self.footer_txt.clear_mode()
                        self.footer_input = False
                    self.footer_txt.advance_stage()
                elif self.footer_txt.mode == 'search':
                    self.search_phrase = self.footer_txt.get_text_value()
                    self.search(self.search_phrase)
            else:
                self.action_ssh(text, self.user)

        # continue searching
        elif input == 'n':
            if self.footer_txt.mode == 'search':
                self.search(self.search_phrase)

        # choose user to ssh
        elif input == 'S':
            self.footer_input = True
            self.set_focus('footer')
            self.footer_txt.set_mode('ssh')

        # Copy files to server
        elif input == '>':
            self.footer_input = True
            self.set_focus('footer')
            self.footer_txt.set_mode('scp_up')

        # Copy files from server
        elif input == '<':
            self.footer_input = True
            self.set_focus('footer')
            self.footer_txt.set_mode('scp_down')

        # Search
        elif input == '/':
            self.footer_input = True
            self.set_focus('footer')
            self.searching = True
            self.search_end = 0
            self.footer_txt.set_mode('search')

        # Quit
        elif input in ('q', 'Q'):
            self.action = 'quit'
            raise urwid.ExitMainLoop()

        # Escape insert mode
        elif input == 'esc':
            self.one_g = False
            self.set_focus('body')
            self.footer_input = False
            self.footer_txt.clear_mode()

####################
# Control stuff
####################

def cmd_list(ec2, environment, options, instances):

    out = instances.values()

    sort = options['sort']
    sort_reverse = False
    if sort[0] == '+':
        sort = sort[1:]
    elif sort[0] == '-':
        sort_reverse = True
        sort = sort[1:]

    out.sort(lambda a, b: 0 + 1 * (a > b) - 1 * (a < b), \
             lambda a: str(a[sort]), sort_reverse)

    # Display the data using urwid
    action, data = ec2_launcher(environment, out).main()
    print action
    print data

    if action == 'ssh':
        instance = instances[data['instance-id']]
        user = data['user']
        cmd_ssh(ec2, environment, options, (user, instance))
        return False

    elif action == 'scp':
        instance = instances[data['instance-id']]
        user = data['user']
        direction = data['direction']
        local_file = data['local_file']
        remote_file = data['remote_file']
        cmd_scp(ec2, environment, options, direction, \
                (local_file, remote_file), (user, instance))
        return False

    elif action == 'refresh':
        return True

    elif action == 'quit':
        return False

def cmd_ssh(ec2, environment, options, (user, instance)):
    ssh = subprocess.Popen( \
            [ "/usr/bin/ssh" \
            , "-q" \
            , "-o" \
            , "StrictHostKeyChecking=no" \
            , "-i" \
            , os.path.join(key_path, environment, "%s.pem" % instance['server-type']) \
            , "%s@%s" % (user, instance['public-dns'])])
    ssh.wait()

def cmd_scp(ec2, environment, options, direction, \
        (local_file, remote_file), (user, instance)):
    remote_file = "%s@%s:%s" % (user, instance['public-dns'], remote_file)
    if direction == 'up':
        source_file = local_file
        dest_file = remote_file
    else:
        source_file = remote_file
        dest_file = local_file
    print " ".join([ "/usr/bin/scp" \
            , "-q" \
            , "-o" \
            , "StrictHostKeyChecking=no" \
            , "-i" \
            , os.path.join(key_path, environment, "%s.pem" % instance['server-type']) \
            , "-r" \
            , source_file \
            , dest_file])
    scp = subprocess.Popen( \
            [ "/usr/bin/scp" \
            , "-q" \
            , "-o" \
            , "StrictHostKeyChecking=no" \
            , "-i" \
            , os.path.join(key_path, environment, "%s.pem" % instance['server-type']) \
            , "-r" \
            , source_file \
            , dest_file])
    scp.wait()

def main(argv):

    result = handle_args(argv)
    if result == 1:
        return 1
    (options, command, environment) = result
    ec2 = start_ec2(environment)
    instances = get_instances(ec2)

    if command == 'list':
        while cmd_list(ec2, environment, options, instances):
            instances = get_instances(ec2)

    elif command == 'ssh':
        instance_id = argv[3]

        m = re.match('(.*?)@(i-[0-9a-f]{8})', instance_id)
        if not m:
            m = re.match('i-[0-9a-f]{8}', instance_id)
            if not m:
                print("Error: syntax is [user@]i-XXXXXXXX")
                return 1
            else:
                user = "root"
        else:
            user = m.group(1)
            instance_id = m.group(2)

        instance = instances[instance_id]
        cmd_ssh(ec2, environment, options, (user, instance))

    elif command == 'scp':

        src = argv[3]
        dest = argv[4]

        remote_regex = re.compile('(.*?)@(i-[0-9a-f]{8}):(.*)')

        m = remote_regex.match(src)
        if m:
            direction = 'down'
            user = m.group(1)
            instance_id = m.group(2)
            remote_file = m.group(3)
            local_file = dest
        else:
            m = remote_regex.match(dest)
            if m:
                direction = 'up'
                user = m.group(1)
                instance_id = m.group(2)
                remote_file = m.group(3)
                local_file = src
            else:
                print("""Error: invalid syntax.  Use either
ec2 %s scp user@i-XXXXXXXX:/path/to/remote/file /path/to/local/file
ec2 %s scp /path/to/local/file user@i-XXXXXXXX:/path/to/remote/file""" \
        % (environment, environment))
                return 1

        instance = instances[instance_id]
        cmd_scp(ec2, environment, options, direction, 
                (local_file, remote_file), (user, instance))
    else:
        raise Exception("Should not have gotten here")

    return 0

sys.exit(main(sys.argv))