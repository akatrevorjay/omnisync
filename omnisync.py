#!/usr/bin/env python
"""The main omnisync module."""

import os
import sys
import logging
import optparse
import time

from version import VERSION
from transports.transportmount import TransportInterface
from urlfunctions import url_splice, url_split


class Configuration:
    """Hold various configuration options."""

    def __init__(self, options):
        """Retrieve the configuration from the parser options."""
        if options.verbosity == 0:
            logging.getLogger().setLevel(logging.ERROR)
        elif options.verbosity == 1:
            logging.getLogger().setLevel(logging.INFO)
        elif options.verbosity == 2:
            logging.getLogger().setLevel(logging.DEBUG)
        self.delete = options.delete
        if options.attributes:
            self.requested_attributes = set(options.attributes)
        else:
            self.requested_attributes = set()
        self.dry_run = options.dry_run
        self.recursive = options.recursive

class OmniSync:
    """The main program class."""
    def __init__(self):
        """Initialise various program structures."""
        self.source = None
        self.destination = None
        self.source_transport = None
        self.destination_transport = None
        self.config = None
        self.max_attributes = None
        self.max_evaluation_attributes = None

        # Initialise the logger.
        logging.basicConfig(level=logging.INFO, format='%(message)s')

        # Import the I/O module classes.
        module_path = "transports"
        for module in os.listdir(module_path):
            if module.endswith("transport.py"):
                module_name = module_path + "." + module[:-3]
                logging.debug("Importing \"%s\"." % (module_name))
                __import__(module_name)

        # Instantiate a dictionary in {"protocol": module} format.
        self.transports = {}
        for transport in TransportInterface.transports:
            for protocol in transport.protocols:
                if protocol in self.transports:
                    logging.warning("Protocol %s already handled, ignoring." % protocol)
                else:
                    self.transports[protocol] = transport

    def check_locations(self):
        """Check that the two locations are suitable for synchronisation."""
        if url_split(self.source).get_dict().keys == ["scheme"]:
            logging.error("You need to specify something more than that for the source.")
            return False
        elif url_split(self.source).get_dict().keys == ["scheme"]:
            logging.error("You need to specify more information than that for the destination.")
            return False
        elif not self.source_transport.exists(self.source):
            logging.error("The source location \"%s\" does not exist, aborting." %
                          self.source)
            return False

        # Check if both locations are of the same type.
        source_isdir = self.source_transport.isdir(self.source)
        leave = False
        if self.source.startswith(self.destination) and source_isdir:
            logging.error("The destination directory is a parent of the source directory.")
            leave = True
        elif not hasattr(self.source_transport, "read"):
            logging.error("The source protocol is write-only.")
            leave = True
        elif not hasattr(self.source_transport, "write"):
            logging.error("The destination protocol is read-only.")
            leave = True
        elif not hasattr(self.destination_transport, "remove") and self.config.delete:
            logging.error("The destination protocol does not support file deletion.")
            leave = True
        elif self.config.requested_attributes - self.source_transport.getattr_attributes:
            logging.error("Requested arguments cannot be read: %s." %
                          ", ".join(x for x in self.config.requested_attributes - \
                                    self.source_transport.getattr_attributes)
                          )
            leave = True
        elif self.config.requested_attributes - self.destination_transport.setattr_attributes:
            logging.error("Requested arguments cannot be set: %s." %
                          ", ".join(x for x in self.config.requested_attributes - \
                                    self.destination_transport.setattr_attributes)
                          )
            leave = True

        if leave:
            return False
        else:
            return True

    def sync(self, source, destination):
        """Synchronise two locations."""
        start_time = time.time()
        self.source = normalise_url(source)
        self.destination = normalise_url(destination)

        # Instantiate the transports.
        try:
            self.source_transport = self.transports[url_split(self.source).scheme]()
        except KeyError:
            logging.error("Protocol not supported: %s." % url_split(self.source).scheme)
            return
        try:
            self.destination_transport = self.transports[url_split(self.destination).scheme]()
        except KeyError:
            logging.error("Protocol not supported: %s." % url_split(self.destination).scheme)
            return

        # Give the transports a chance to connect to their servers.
        self.source_transport.connect(self.source)
        self.destination_transport.connect(self.destination)

        # These are the most attributes we can expect from getattr calls in these two protocols.
        self.max_attributes = (self.source_transport.getattr_attributes &
                               self.destination_transport.getattr_attributes)

        self.max_evaluation_attributes = (self.source_transport.evaluation_attributes &
                                          self.destination_transport.evaluation_attributes)

        if not self.check_locations():
            return

        # Begin the actual synchronisation.
        self.recurse()

        self.source_transport.disconnect()
        self.destination_transport.disconnect()
        logging.info("Finished in %.2f sec." % (time.time() - start_time))

    def set_destination_attributes(self, destination, attributes):
        """Set the destination's attributes. This is a wrapper for the transport's _setattr_."""
        # The given attributes might not have any we're able to set, so just return if that's
        # the case.
        if not self.config.dry_run and \
           set(attributes) & set(self.destination_transport.setattr_attributes):
            self.destination_transport.setattr(destination, attributes)

    def recurse(self):
        """Recursively synchronise everything."""
        source_dir_list = self.source_transport.listdir(self.source)
        # If the source is a file, rather than a directory, just copy it. We know for sure that
        # it exists from the checks we did before, so the "False" return value can't be because
        # of that.
        if not source_dir_list:
            dest_isdir = self.destination_transport.isdir(self.destination)
            # If the destination ends in a slash or is an actual directory:
            if self.destination.endswith("/") or \
               not self.destination.endswith("/") and dest_isdir:
                if not dest_isdir:
                    self.destination_transport.mkdir(self.destination)
                # Splice the source filename onto the destination URL.
                dest_url = url_splice(url_split(self.source)[0], self.source, self.destination)
                self.compare_and_copy(self.source, dest_url)
            else:
                self.compare_and_copy(self.source, self.destination)
        else:
            directory_queue = source_dir_list

            # Depth-first tree traversal.
            while directory_queue:
                url, attrs = directory_queue.pop()
                if "isdir" not in attrs:
                    attrs["isdir"] = self.source_transport.isdir(url)
                logging.debug("URL %s is %sa directory." % \
                              (url, not attrs["isdir"] and "not " or ""))
                if attrs["isdir"]:
                    directory_queue.extend(self.source_transport.listdir(url))
                else:
                    dest_url = url_splice(self.source, url, self.destination)
                    logging.debug("Destination URL is %s." % dest_url)
                    self.compare_and_copy(url, dest_url, attrs, {})

    def compare_and_copy(self, source, destination, src_attrs=None, dest_attrs=None):
        """Compare the attributes of two files and copy if changed.

           source            - A source URL of a file.
           destination       - A destination URL of a file.
           src_attrs - A dictionary containing some source attributes.

           Returns True if the file was copied, False otherwise.
        """
        if src_attrs is None:
            src_attrs = {}
        if dest_attrs is None:
            dest_attrs = {}

        # Try to gather as many attributes of both files as possible.
        our_src_attributes = (set(src_attrs) & self.max_evaluation_attributes)
        max_src_attributes = (self.source_transport.getattr_attributes &
                              self.max_evaluation_attributes) | \
                              self.config.requested_attributes
        src_difference = max_src_attributes - our_src_attributes
        if src_difference:
            # If the set of useful attributes we have is smaller than the set of attributes the
            # user requested and the ones we can gather through getattr(), get the rest.
            logging.debug("Source getattr for file %s and arguments %s deemed necessary." % \
                          (source, src_difference))
            src_attrs.update(self.source_transport.getattr(source, src_difference))
            # We should now have all the attributes we're interested in, both for evaluating if
            # the files are different and setting.

        # We aren't interested in the user's requested arguments for the destination.
        dest_difference = (self.destination_transport.getattr_attributes - set(dest_attrs)) & \
                           self.max_evaluation_attributes
        if dest_difference:
            # Same for the destination.
            logging.debug("Destination getattr for %s deemed necessary." % destination)
            dest_attrs.update(self.destination_transport.getattr(destination, dest_difference))

        # Compare the evaluation keys that are common in both dictionaries. If one is different,
        # copy the file.
        evaluation_attributes = set(src_attrs) & set(dest_attrs) & self.max_evaluation_attributes
        logging.debug("Checking evaluation attributes %s..." % evaluation_attributes)
        for key in evaluation_attributes:
            if src_attrs[key] != dest_attrs[key]:
                logging.debug("Source and destination %s was different (%s vs %s)." %\
                              (key, src_attrs[key], dest_attrs[key]))
                logging.info("Copying \"%s\" to \"%s\"..." % (source, destination))
                try:
                    self.copy_file(source, destination)
                except IOError:
                    return
                else:
                    # If the file was successfully copied, set its attributes.
                    self.set_destination_attributes(destination, src_attrs)
                    return
        else:
            # The two files are identical, skip them...
            logging.info("Files \"%s\" and \"%s\" are identical, skipping..." %
                         (source, destination))
            # ...but set the attributes anyway.
            self.set_destination_attributes(destination, src_attrs)

    def copy_file(self, source, destination):
        """Copy a file."""
        if self.config.dry_run:
            return

        # Select the smallest buffer size of the two, to avoid congestion.
        buffer_size = min(self.source_transport.buffer_size,
                          self.destination_transport.buffer_size)
        try:
            self.source_transport.open(source, "rb")
        except IOError:
            logging.error("Could not open %s, skipping..." % source)
            raise
        # TODO: This is an ugly, ugly hack, remove when we improve it.
        self.destination_transport.mkdir(destination[:destination.rfind("/")])
        # Remove the file before copying.
        self.destination_transport.remove(destination)
        try:
            self.destination_transport.open(destination, "wb")
        except IOError:
            logging.error("Could not open %s, skipping..." % destination)
            self.destination_transport.close()
            self.source_transport.close()
            raise
        data = self.source_transport.read(buffer_size)
        while data:
            self.destination_transport.write(data)
            data = self.source_transport.read(buffer_size)
        self.destination_transport.close()
        self.source_transport.close()


omnisync = OmniSync()

def normalise_url(url):
    """Normalise a URL from its shortcut to its proper form."""
    # Replace all backslashes with forward slashes.
    url = url.replace("\\", "/")

    # Prepend file:// to the URL if it lacks a protocol.
    split_url = url_split(url)
    if split_url.scheme == "":
        url = "file://" + url
    return url

def parse_arguments():
    """Parse the command-line arguments."""
    parser = optparse.OptionParser(
        usage="%prog [options] <source> <destination>",
        version="%%prog %s" % VERSION
        )
    parser.set_defaults(verbosity=1)
    parser.add_option("-q", "--quiet",
                      action="store_const",
                      dest="verbosity",
                      const=0,
                      help="be vewy vewy quiet"
                      )
    parser.add_option("-d", "--debug",
                      action="store_const",
                      dest="verbosity",
                      const=2,
                      help="talk too much"
                      )
    parser.add_option("-r", "--recursive",
                      action="store_true",
                      dest="recursive",
                      help="recurse into directories"
                      )
    parser.add_option("--delete",
                      action="store_true",
                      dest="delete",
                      help="delete extraneous files from destination dirs"
                      )
    parser.add_option("-n", "--dry-run",
                      action="store_true",
                      dest="dry_run",
                      help="show what would have been transferred"
                      )
    parser.add_option("-p", "--perms",
                      action="append_const",
                      const="perms",
                      dest="attributes",
                      help="preserve permissions"
                      )
    parser.add_option("-o", "--owner",
                      action="append_const",
                      const="owner",
                      dest="attributes",
                      help="preserve owner"
                      )
    parser.add_option("-g", "--group",
                      action="append_const",
                      const="group",
                      dest="attributes",
                      help="preserve group"
                      )
    (options, args) = parser.parse_args()
    if len(args) != 2:
        parser.print_help()
        sys.exit()
    return options, args

def run():
    """Run the program."""
    (options, args) = parse_arguments()
    omnisync.config = Configuration(options)
    omnisync.sync(args[0], args[1])

if __name__ == "__main__":
    run()
