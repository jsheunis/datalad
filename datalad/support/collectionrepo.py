# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Implements datalad collection repositories.
"""

import logging
import os
from os.path import join as opj, basename, exists
from six import string_types, itervalues

from rdflib import Graph, URIRef

from .exceptions import CollectionBrokenError
from .gitrepo import GitRepo
from .handle import Handle
from .handlerepo import HandleRepo
from .metadatahandler import CustomImporter, DLNS, RDFS, Literal, \
    MetadataImporter, DCTERMS, RDF
from ..consts import REPO_STD_META_FILE, REPO_CONFIG_FILE
from ..utils import get_local_file_url

lgr = logging.getLogger('datalad.collectionrepo')


class CollectionRepo(GitRepo):
    """Representation of a datalad collection repository.

    A Collection is represented as a git-repository containing:
        a) a file named 'datalad.ttl', which stores datalad standard metadata
           of the collection itself, and a file named 'config.ttl', datalad
           uses to operate on that collection
        b) Optionally additional ttl-files for additional collection level
           metadata
        c) one directory per handle, storing the metadata of each handle in the
           same way as the collection level metadata.

    Attention: files are valid only if in git.
    Being present is not sufficient!
    """

    # TODO: not up-to-date:
    # __slots__ = GitRepo.__slots__ + ['name']

    def __init__(self, path, url=None, name=None, runner=None, create=True):
        """

        Parameters
        ----------
        path: str
          path to git repository. In case it's not an absolute path, it's
          relative to PWD

        url: str
          url to the to-be-cloned repository. Requires valid git url
          according to
          http://www.kernel.org/pub/software/scm/git/docs/git-clone.html#URLS .

        name: str
          optional name of the collection. This is only used for creating new
          collections. If there is a collection repo at path already, `name`
          is ignored.

        create: bool
          if true, creates a collection repository at path, in case there is
          none. Otherwise an exception is raised.

        Raises
        ------
        CollectionBrokenError
        """

        super(CollectionRepo, self).__init__(path, url, runner=runner,
                                             create=create)

        importer = CustomImporter('Collection', 'Collection', DLNS.this)
        # load existing files:

        if REPO_CONFIG_FILE in self.get_indexed_files():
            importer.import_data(opj(self.path, REPO_CONFIG_FILE))
        elif not create:
            raise CollectionBrokenError("Missing %s in git: %s." %
                                        (REPO_CONFIG_FILE, self.path))
        if REPO_STD_META_FILE in self.get_indexed_files():
            importer.import_data(opj(self.path, REPO_STD_META_FILE))
        elif not create:
            raise CollectionBrokenError("Missing %s in git: %s." %
                                        (REPO_STD_META_FILE, self.path))
        graphs = importer.get_graphs()

        # collection settings:
        # if there is no name statement, add it:
        self_uri = graphs[REPO_CONFIG_FILE[0:-4]].value(predicate=RDF.type,
                                                        object=DLNS.Collection)

        if len([subj for subj in
                graphs[REPO_CONFIG_FILE[0:-4]].objects(subject=self_uri,
                                                       predicate=RDFS.label)]) \
                == 0:
            if create:
                graphs[REPO_CONFIG_FILE[0:-4]].add(
                    (self_uri, RDFS.label,
                     Literal(name or basename(self.path))))
            else:
                raise CollectionBrokenError("Missing label in %s." %
                                            opj(self.path, REPO_CONFIG_FILE))

        if create:
            importer.set_graphs(graphs)  # necessary?
            importer.store_data(self.path)
            self.git_add([REPO_CONFIG_FILE, REPO_STD_META_FILE])

            if not self.repo.head.is_valid() or \
                    self.repo.index.diff(self.repo.head.commit):
                self.git_commit("Initialized collection metadata.")

    def __repr__(self):
        return "<CollectionRepo path=%s (%s)>" % (self.path, type(self))

    def _get_cfg(self):
        config_handler = CustomImporter('Collection', 'Collection', DLNS.this)
        config_handler.import_data(opj(self.path, REPO_CONFIG_FILE))
        return config_handler.get_graphs()[REPO_CONFIG_FILE[0:-4]]

    def _set_cfg(self, graph, commit_msg="Updated config file."):
        config_handler = CustomImporter('Collection', 'Collection', DLNS.this)
        graph_dict = dict()
        graph_dict[REPO_CONFIG_FILE[0:-4]] = graph
        config_handler.set_graphs(graph_dict)
        config_handler.store_data(self.path)
        self.git_add(REPO_CONFIG_FILE)
        self.git_commit(commit_msg)

    # TODO: Consider using preferred label for the name
    def get_name(self):
        graph = self._get_cfg()
        self_uri = graph.value(predicate=RDF.type, object=DLNS.Collection)
        return str(self._get_cfg().value(subject=self_uri,
                                         predicate=RDFS.label))

    def set_name(self, name):
        graph = self._get_cfg()
        self_uri = graph.value(predicate=RDF.type, object=DLNS.Collection)
        for old_name in graph.objects(self_uri, RDFS.label):
            graph.remove((self_uri, RDFS.label, old_name))
        graph.add((self_uri, RDFS.label, Literal(name)))
        self._set_cfg(graph, "Changed name.")

    name = property(get_name, set_name)

    def get_handle_list(self, branch=None):
        """Get a list of the names of the handles in `branch`

        If `branch` is not provided, the active one is used.
        """

        if branch is None:
            branch = self.git_get_active_branch()

        return list(set([self._filename2key(f.split(os.sep)[0])
                         for f in self.git_get_files(branch)
                         if f != basename(f) and not f.startswith('.')]))

    def _filename2key(self, fname):
        """Placeholder

        This transformation of a filename to a handle's key may change,
        especially with respect to unicode keys.
        """
        return fname.replace('--', '/')

    def _key2filename(self, key):
        """Placeholder

        This transformation of a handle's key to a filename may change.
        Note: No '\' allowed in handle or collection names. '--' is treated
        as '/'.
        """
        if '\\' in key:
            raise ValueError("Invalid name: '%s'. No '\\' allowed.")
        if key in self.get_handle_list():
            return key.replace('/', '--')

        parts = key.split('/')
        if parts[0] in self.git_get_remotes() \
                or parts[0] == self.name \
                or parts[0] == self.git_get_active_branch():
            return key[len(parts[0]) + 1:].replace('/', '--')
        else:
            return key.replace('/', '--')

    # ### repo methods:

    def get_handles(self, branch=None):
        """Get a list of Handle instances.

        This is different to get_handle_list(), which provides a list of handle
        names.
        """
        from datalad.support.handle_backends import CollectionRepoHandleBackend
        return [CollectionRepoHandleBackend(self, key=key,
                                            branch=branch)
                for key in self.get_handle_list(branch=branch)]

    def get_handle_repos(self, branch=None):
        """Get a list of HandleRepo instances.

        Returns a list containing HandleRepo instances representing locally
        available handle repositories, that are part of the collection
        in `branch`. If no branch is given, the active branch is used.
        """
        # repo_list = []
        # for handle in self._get_handle_files(branch):
        #     parser = SafeConfigParser()
        #     parser.read(opj(self.path, handle))
        #     path = parser.get('Handle', 'last_seen')
        #     if exists(path):
        #         repo_list.append(HandleRepo(path))
        # return repo_list
        raise NotImplementedError

    def get_handle_repo(self, key):
        """Get a HandleRepo instance for handle `key`.

        If the handle isn't locally available, returns None.
        """
        # TODO: branch-option?
        # parser = SafeConfigParser()
        # parser.read(opj(self.path, self._key2filename(key)))
        # path = parser.get('Handle', 'last_seen')
        # if exists(path):
        #     return HandleRepo(path)
        # else:
        #     return None
        raise NotImplementedError

    def get_backend_from_branch(self, branch=None):
        """Convenience function to get a backend from a branch of this repo.

        By default a backend to the active branch is returned.
        """
        if branch is None:
            branch = self.git_get_active_branch()
        from datalad.support.collection_backends import CollectionRepoBackend
        return CollectionRepoBackend(self, branch)

    def _import_metadata(self, target_path, importer, about_uri,
                         about_class=None, files=None, data=None):
        """Imports metadata to the collection.

        Private method to be used by import_metadata_to_handle and
        import_metadata_to_collection.
        Expects either `files` or `data` to be not `None`.

        Parameters
        __________
        target_path: str
          path to the metadata files. Depends on whether to add to collection
          level metadata or a handle of the collection
        importer: class
          the importer to be used; has to be a subclass of MetadataImporter
        about_uri: URIRef
          uri of the entity the metadata is about
        about_class: str
          Either "Handle", "Collection" or None, in case of a sub-entity
        files: str or list of str
          either a path to the file or directory to be imported or a list
          containing paths to the files.
        data: dict of list of str
          a dictionary containing the metadata to be imported. The key is
          expected to be the file name and the value its content as a list of
          the file's lines as returned by `readlines()`.
        """
        if not issubclass(importer, MetadataImporter):
            raise TypeError("Not a MetadataImporter: " + str(importer))

        # TODO: if about_uri doesn't exist and it's a subentity
        # (about_class is None), create it. (+ doc)

        # TODO: check whether cfg-file even exists, otherwise create
        # a basic one.
        cfg_graph = Graph().parse(opj(target_path, REPO_CONFIG_FILE),
                                  format="turtle")

        # check for existing metadata sources to determine the name for the
        # new one:

        known_branches = self.git_get_branches()
        known_sources = [src for src in cfg_graph.objects(about_uri,
                                                          DLNS.usesSrc)]

        from six import PY3
        if PY3:
            src_name_prefix = "".join(filter(str.isalnum, str(about_uri)))
        else:
            src_name_prefix = filter(str.isalnum, str(about_uri))
        from random import choice
        from string import ascii_letters
        from six.moves import xrange
        while True:
            src_name = src_name_prefix + "_import_" + \
                       ''.join(choice(ascii_letters) for i in xrange(6))
            if src_name not in known_branches and \
                        URIRef(src_name) not in known_sources:
                break

        if files is not None and data is None:
            # treat it as a metadata source, that can be used again later on.
            src_node = URIRef(src_name)
            # add config-entries for that source:
            cfg_graph.add((about_uri, DLNS.usesSrc, src_node))
            if isinstance(files, string_types):
                cfg_graph.add((src_node, DLNS.usesFile, URIRef(files)))
            elif isinstance(files, list):
                [cfg_graph.add((src_node, DLNS.usesFile, URIRef(f)))
                 for f in files]
            remove_import_branch = False

        elif files is None and data is not None:
            # just metadata to read, nothing we can refer to later on
            # Therefore remove the import branch afterwards
            remove_import_branch = True
        else:
            raise ValueError("Either 'files' or 'data' have to be passed.")

        im = importer(target_class='Collection', about_class=about_class,
                      about_uri=about_uri)
        im.import_data(files=files, data=data)

        # add new config statements:
        graphs = im.get_graphs()
        graphs[REPO_CONFIG_FILE[:-4]] += cfg_graph

        # replace possible "this"-statements in metadata source by the now used
        # 'about_uri':
        if about_uri != DLNS.this:
            for g in graphs:
                for s, p, o in graphs[g]:
                    if s == DLNS.this:
                        graphs[g].add((about_uri, p, o))
                        graphs[g].remove((s, p, o))

        # create import branch:
        active_branch = self.git_get_active_branch()
        self.git_checkout(name=src_name, options='-b')

        im.store_data(target_path)
        self.git_add(target_path)
        self.git_commit("New import branch created.")

        # switching back and merge:
        # Note: -f used for the same reason as in remove_handle
        # TODO: Check this out
        self.git_checkout(active_branch, options="-f")
        self.git_merge(src_name)

        if remove_import_branch:
            self.git_remove_branch(src_name)

    def import_metadata_to_handle(self, importer, key, files=None, data=None,
                                  about_uri=None):
        # TODO: Doc

        target_path = opj(self.path, self._key2filename(key))

        if about_uri is None:
            # by default the metadata is assumed to be about the handle itself:
            about_uri = Graph().parse(opj(target_path, REPO_CONFIG_FILE),
                                      format="turtle").value(
                predicate=RDFS.label,
                object=Literal(key))
            about_class = "Handle"
        else:
            # Assume it's a sub-entity.
            # Note: Theoretically, we could recognize, whether or not this uri
            # is matching the uri of the handle
            about_class = None

        self._import_metadata(target_path=target_path, importer=importer,
                              about_uri=about_uri, about_class=about_class,
                              files=files, data=data)

    def import_metadata_collection(self, importer, files=None, data=None,
                                   about_uri=None):

        # TODO: Doc
        if about_uri is None:
            about_uri = DLNS.this
            about_class = "Collection"
        else:
            # Assume it's a sub-entity.
            # Note: Theoretically, we could recognize, whether or not this uri
            # is matching the uri of the collection
            about_class = None

        self._import_metadata(target_path=self.path, importer=importer,
                              about_uri=about_uri, about_class=about_class,
                              files=files, data=data)

    def update_metadata_src(self):
        # TODO
        raise NotImplementedError

    def remove_metadata_src(self):
        # Do we need that one?
        raise NotImplementedError

    def add_handle(self, handle, name=None):
        """Adds a handle to the collection repository.

        Parameters
        ----------
        handle: str or HandleRepo or Handle
          URL of the handle or an instance of either HandleRepo or Handle.
        name: str
          name of the handle within the collection. This name is required to be
          unique with respect to the collection. If a HandleRepo or
          Handle is passed, the name stored therein is the default.
          If `handle` is just an URL, a name is required.
        """
        if isinstance(handle, Handle):
            uri = URIRef(handle.url)
            name = name or handle.name

        if isinstance(handle, HandleRepo):
            uri = URIRef(get_local_file_url(handle.path))
            name = name or handle.name

        if isinstance(handle, string_types):
            uri = URIRef(handle)
            if name is None:
                raise ValueError("Argument 'name' is None.")

        # TODO: What about branch? For now, just active one.
        # This means, the branch to add the handle to, has to be checked out.
        # Is there a point in providing anything else?
        branch = self.git_get_active_branch()
        key = self.name + '/' + name

        path = opj(self.path, self._key2filename(key))

        # TODO: What's the desired behaviour in case a handle with that name
        # already exists?
        if exists(path):
            raise RuntimeError("Handle '%s' already exists." % name)
        os.mkdir(path)

        md_handle = CustomImporter(target_class='Collection',
                                   about_class='Handle',
                                   about_uri=uri)
        graphs = md_handle.get_graphs()

        # handle config:
        # default name:
        graphs[REPO_CONFIG_FILE[0:-4]].add((uri, RDFS.label, Literal(name)))
        # default dir name:
        graphs[REPO_CONFIG_FILE[0:-4]].add((uri, DLNS.defaultTarget, Literal(name)))
        # TODO: Is this target actually correct?
        # TODO: anything else?
        md_handle.set_graphs(graphs)
        md_handle.store_data(path)

        # collection graph:
        # TODO: helper functions
        md_collection = CustomImporter(target_class='Collection',
                                       about_class='Collection',
                                       about_uri=DLNS.this)
        md_collection.import_data(self.path)
        graphs = md_collection.get_graphs()
        graphs[REPO_STD_META_FILE[0:-4]].add((DLNS.this, DCTERMS.hasPart, uri))
        # TODO: anything else? any config needed?
        md_collection.set_graphs(graphs)
        md_collection.store_data(self.path)

        self.git_add([REPO_STD_META_FILE, REPO_CONFIG_FILE,
                      self._key2filename(key)])
        self.git_commit("Added handle '%s'" % name)

    def remove_handle(self, key):

        dir_ = self._key2filename(key)

        # remove handle from collection descriptor:
        uri = Graph().parse(opj(self.path, dir_, REPO_STD_META_FILE),
                            format="turtle").value(predicate=RDF.type,
                                                   object=DLNS.Handle)
        col_graph = Graph().parse(opj(self.path, REPO_STD_META_FILE),
                                  format="turtle")
        col_graph.remove((DLNS.this, DCTERMS.hasPart, uri))
        col_graph.serialize(opj(self.path, REPO_STD_META_FILE), format="turtle")

        # remove handle's directory:
        # Note: Currently all files separatly due to issues with the
        # normalize_path decorator in gitrepo.py. It expects one output per
        # one input file. So, recursively removing the 'dir_' violates that
        # assertion.
        # Note2: Currently using "-f" option, since on ntfs/vfat, git somehow
        # reports the files (at least config.ttl) have staged changes.
        # TODO: Figure out, what the hell this is about.
        [self.git_remove(file_, f=True) for file_ in self.get_indexed_files()
         if file_.startswith(dir_)]

        self.git_add(REPO_STD_META_FILE)
        self.git_commit("Removed handle %s." % key)

    def get_handle_graphs(self, key, branch=None, files=None):
        """

        :param key:
        :param branch:
        :param files:
        :return:
        """

        if branch is None:
            branch = self.git_get_active_branch()

        # by default read all ttl-files except handle's REPO_CONFIG_FILE
        if files is None:
            files = [file_
                     for file_ in self.git_get_files(branch=branch)
                     if file_.startswith(self._key2filename(key)) and
                     file_.endswith(".ttl")]
                     # exclude config?
                     # and basename(file_) != REPO_CONFIG_FILE]

        graphs = dict()
        for file_ in files:
            # TODO: opj(_key2filname(key), file_)?
            file_str = '\n'.join(self.git_get_file_content(file_, branch))
            graphs[basename(file_).rstrip(".ttl")] = \
                Graph().parse(data=file_str, format="turtle")
        return graphs

    def store_handle_graphs(self, graphs, handle, branch=None,
                            msg="Handle metadata updated."):
        # TODO: Default message to include handle's name

        if not isinstance(graphs, dict):
            raise TypeError("Unexpected type of parameter 'graphs' (%s). "
                            "Expected: dict." % type(graphs))

        files = dict()
        for key in graphs:
            if not isinstance(graphs[key], Graph):
                raise TypeError("Wrong type of graphs['%s'] (%s). "
                                "Expected: Graph." % (key, type(graphs[key])))

            files[key] = opj(self.path, self._key2filename(handle), key + ".ttl")

        active_branch = self.git_get_active_branch()
        if branch is None:
            branch = active_branch
        elif branch not in self.git_get_branches():
            raise ValueError("Unknown branch '%s'." % branch)
        else:
            self.git_checkout(branch)

        for key in graphs:
            graphs[key].serialize(files[key], format="turtle")

        self.git_add(list(itervalues(files)))
        self.git_commit(msg)

        if branch != active_branch:
            self.git_checkout(active_branch)

    def get_collection_graphs(self, branch=None, files=None):

        if branch is None:
            branch = self.git_get_active_branch()

        # by default read all ttl-files in base dir:
        if files is None:
            files = [file_
                     for file_ in self.git_get_files(branch=branch)
                     if os.sep not in file_ and
                     file_.endswith(".ttl")]
                    # Note: Think of additionally exclude config.ttl:
                    # and file_ != REPO_CONFIG_FILE

        graphs = dict()
        for file_ in files:
            file_str = '\n'.join(self.git_get_file_content(file_, branch))
            graphs[file_.rstrip(".ttl")] = \
                Graph().parse(data=file_str, format="turtle")
        return graphs

    def set_collection_graphs(self, graphs, branch=None,
                              msg="Collection metadata updated."):

        if not isinstance(graphs, dict):
            raise TypeError("Unexpected type of parameter 'graphs' (%s). "
                            "Expected: dict." % type(graphs))

        files = dict()
        for key in graphs:
            if not isinstance(graphs[key], Graph):
                raise TypeError("Wrong type of graphs['%s'] (%s). "
                                "Expected: Graph." % (key, type(graphs[key])))

            files[key] = opj(self.path, key + ".ttl")

        active_branch = self.git_get_active_branch()
        if branch is None:
            branch = active_branch
        elif branch not in self.git_get_branches():
            raise ValueError("Unknown branch '%s'." % branch)
        else:
            self.git_checkout(branch)

        for key in graphs:
            graphs[key].serialize(files[key], format="turtle")

        self.git_add(list(itervalues(files)))
        self.git_commit(msg)

        if branch != active_branch:
            self.git_checkout(active_branch)

