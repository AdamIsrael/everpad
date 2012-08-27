import sys
sys.path.append('../..')
from PySide.QtCore import QThread, Slot, QTimer, Signal, QWaitCondition, QMutex
from evernote.edam.type.ttypes import (
    Note, Notebook, Tag, NoteSortOrder,
    Resource, Data, ResourceAttributes,
)
from evernote.edam.notestore.ttypes import NoteFilter
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy import and_
from evernote.edam.limits.constants import (
    EDAM_NOTE_TITLE_LEN_MAX, EDAM_NOTE_CONTENT_LEN_MAX,
    EDAM_TAG_NAME_LEN_MAX, EDAM_NOTEBOOK_NAME_LEN_MAX,
    EDAM_USER_NOTES_MAX,
)
from evernote.edam.error.ttypes import EDAMUserException
from everpad.provider.tools import (
    ACTION_NONE, ACTION_CREATE,
    ACTION_CHANGE, ACTION_DELETE,
    get_db_session, get_note_store,
)
from everpad.tools import get_auth_token
from everpad.provider import models
from everpad.const import STATUS_NONE, STATUS_SYNC, DEFAULT_SYNC_DELAY
from base64 import b64encode, b64decode
from datetime import datetime
import time
SYNC_MANUAL = -1


class SyncThread(QThread):
    force_sync_signal = Signal()
    """Sync notes with evernote thread"""
    def __init__(self, app, *args, **kwargs):
        QThread.__init__(self, *args, **kwargs)
        self.app = app
        self.status = STATUS_NONE
        self.last_sync = datetime.now()
        self.timer = QTimer()
        self.timer.timeout.connect(self.sync)
        self.update_timer()
        self.wait_condition = QWaitCondition()
        self.mutex = QMutex()

    def update_timer(self):
        self.timer.stop()
        delay = int(self.app.settings.value('sync_delay') or 0) or DEFAULT_SYNC_DELAY
        if delay != SYNC_MANUAL:
            self.timer.start(delay)

    def run(self):
        self.session = get_db_session()
        self.sq = self.session.query
        self.auth_token = get_auth_token()
        self.note_store = get_note_store(self.auth_token)
        self.perform()
        while True:
            self.mutex.lock()
            self.wait_condition.wait(self.mutex)
            self.perform()
            self.mutex.unlock()

    def force_sync(self):
        self.timer.stop()
        self.sync()
        self.update_timer()

    @Slot()
    def sync(self):
        self.wait_condition.wakeAll()

    def perform(self):
        """Perform all sync"""
        self.status = STATUS_SYNC
        self.last_sync = datetime.now()
        try:
            self.local_changes()
            self.remote_changes()
        except Exception, e:  # maybe log this
            print e
            self.session.rollback()
        finally:
            self.status = STATUS_NONE

    def local_changes(self):
        """Send local changes to evernote server"""
        self.notebooks_local()
        self.tags_local()
        self.notes_local()

    def remote_changes(self):
        """Receive remote changes from evernote"""
        self.notebooks_remote()
        self.tags_remote()
        self.notes_remote()

    def notebooks_local(self):
        """Send local notebooks changes to server"""
        for notebook in self.sq(models.Notebook).filter(
            models.Notebook.action != ACTION_NONE,
        ):
            kwargs = dict(
                name=notebook.name[:EDAM_NOTEBOOK_NAME_LEN_MAX].strip().encode('utf8'),
                defaultNotebook=notebook.default,
            )
            if notebook.guid:
                kwargs['guid'] = notebook.guid
            nb = Notebook(**kwargs)
            if notebook.action == ACTION_CHANGE:
                while True:
                    try:
                        nb = self.note_store.updateNotebook(
                            self.auth_token, nb,
                        )
                        break
                    except EDAMUserException, e:
                        notebook.name = notebook.name + '*'  # shit, but work
                        print e
            elif notebook.action == ACTION_CREATE:
                nb = self.note_store.createNotebook(
                    self.auth_token, nb,
                )
                notebook.guid = nb.guid
            elif notebook.action == ACTION_DELETE and False:  # not allowed for app now
                try:
                    self.note_store.expungeNotebook(
                        self.auth_token, notebook.guid,
                    )
                    self.session.delete(notebook)
                except EDAMUserException, e:
                    print e
            notebook.action = ACTION_NONE
        self.session.commit()

    def tags_local(self):
        """Send loacl tags changes to server"""
        for tag in self.sq(models.Tag).filter(
            models.Tag.action != ACTION_NONE,
        ):
            kwargs = dict(
                name=tag.name[:EDAM_TAG_NAME_LEN_MAX].strip().encode('utf8'),
            )
            if tag.guid:
                kwargs['guid'] = tag.guid
            tg = Tag(**kwargs)
            if tag.action == ACTION_CHANGE:
                tg = self.note_store.updateTag(
                    self.auth_token, tg,
                )
            elif tag.action == ACTION_CREATE:
                tg = self.note_store.createTag(
                    self.auth_token, tg,
                )
                tag.guid = tg.guid
            tag.action = ACTION_NONE
        self.session.commit()

    def notes_local(self):
        """Send loacl notes changes to server"""
        for note in self.sq(models.Note).filter(
            models.Note.action != ACTION_NONE,
        ):
            kwargs = dict(
                title=note.title[:EDAM_NOTE_TITLE_LEN_MAX].strip().encode('utf8'),
                content= (u"""
                    <!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">
                    <en-note>%s</en-note>
                """ % note.content[:EDAM_NOTE_CONTENT_LEN_MAX]).strip().encode('utf8'),
                tagGuids=map(
                    lambda tag: tag.guid, note.tags,
                ),
            )
            if note.notebook:
                kwargs['notebookGuid'] = note.notebook.guid
            if note.guid:
                kwargs['guid'] = note.guid
            nt = Note(**kwargs)
            if note.action == ACTION_CHANGE:
                nt.resources = map(lambda res: Resource(
                    noteGuid=note.guid,
                    data=Data(body=open(res.file_path).read()),
                    mime=res.mime,
                    attributes=ResourceAttributes(
                        fileName=res.file_name.encode('utf8'),
                    ),
                ), self.sq(models.Resource).filter(and_(
                    models.Resource.note_id == note.id, 
                    models.Resource.action != models.ACTION_DELETE,
                )))
                nt = self.note_store.updateNote(self.auth_token, nt)
            elif note.action == ACTION_CREATE:
                nt = self.note_store.createNote(self.auth_token, nt)
                note.guid = nt.guid
            elif note.action == ACTION_DELETE:
                try:
                    self.note_store.deleteNote(self.auth_token, nt.guid)
                    self.session.delete(note)
                except EDAMUserException:
                    pass
            note.action = ACTION_NONE
        self.session.commit()

    def notebooks_remote(self):
        """Receive notebooks from server"""
        notebooks_ids = []
        for notebook in self.note_store.listNotebooks(self.auth_token):
            try:
                nb = self.sq(models.Notebook).filter(
                    models.Notebook.guid == notebook.guid,
                ).one()
                notebooks_ids.append(nb.id)
                if nb.service_updated < notebook.serviceUpdated:
                    nb.from_api(notebook)
            except NoResultFound:
                nb = models.Notebook(guid=notebook.guid)
                nb.from_api(notebook)
                self.session.add(nb)
                self.session.commit()
                notebooks_ids.append(nb.id)
        if len(notebooks_ids):
            self.sq(models.Notebook).filter(
                ~models.Notebook.id.in_(notebooks_ids),
            ).delete(synchronize_session='fetch')
        self.session.commit()

    def tags_remote(self):
        """Receive tags from server"""
        tags_ids = []
        for tag in self.note_store.listTags(self.auth_token):
            try:
                tg = self.sq(models.Tag).filter(
                    models.Tag.guid == tag.guid,
                ).one()
                tags_ids.append(tg.id)
                if tg.name != tag.name.decode('utf8'):
                    tg.from_api(tag)
            except NoResultFound:
                tg = models.Tag(guid=tag.guid)
                tg.from_api(tag)
                self.session.add(tg)
                self.session.commit()
                tags_ids.append(tg.id)
        if len(tags_ids):
            self.sq(models.Tag).filter(
                ~models.Tag.id.in_(tags_ids)
            ).delete(synchronize_session='fetch')
        self.session.commit()

    def _iter_all_notes(self):
        """Iterate all notes"""
        offset = 0
        while True:
            note_list = self.note_store.findNotes(self.auth_token, NoteFilter(
                order=NoteSortOrder.UPDATED,
                ascending=False,
            ), offset, EDAM_USER_NOTES_MAX)
            for note in note_list.notes:
                yield note
            offset = note_list.startIndex + len(note_list.notes)
            if note_list.totalNotes - offset <= 0:
                break

    def notes_remote(self):
        """Receive notes from server"""
        notes_ids = []
        for note in self._iter_all_notes():
            try:
                nt = self.sq(models.Note).filter(
                    models.Note.guid == note.guid,
                ).one()
                notes_ids.append(nt.id)
                if nt.updated < note.updated:
                    note = self.note_store.getNote(
                        self.auth_token, note.guid,
                        True, True, True, True,
                    )
                    nt.from_api(note, self.session)
                    self.note_resources_remote(note, nt)
            except NoResultFound:
                note = self.note_store.getNote(
                    self.auth_token, note.guid,
                    True, True, True, True,
                )
                nt = models.Note(guid=note.guid)
                nt.from_api(note, self.session)
                self.session.add(nt)
                self.session.commit()
                notes_ids.append(nt.id)
                self.note_resources_remote(note, nt)
        if len(notes_ids):
            self.sq(models.Note).filter(
                ~models.Note.id.in_(notes_ids)
            ).delete(synchronize_session='fetch')        
        self.session.commit()

    def note_resources_remote(self, note_api, note_model):
        resources_ids = []
        for resource in note_api.resources or []:
            try:
                rs = self.sq(models.Resource).filter(
                    models.Resource.guid == resource.guid,
                ).one()
                resources_ids.append(rs.id)
                if b64decode(rs.hash) != resource.data.bodyHash:
                    rs.from_api(resource)
            except NoResultFound:
                rs = models.Resource(
                    guid=resource.guid,
                    note_id=note_model.id,
                )
                rs.from_api(resource)
                self.session.add(rs)
                self.session.commit()
                resources_ids.append(rs.id)
        if len(resources_ids):
            self.sq(models.Resource).filter(and_(
                ~models.Resource.id.in_(resources_ids),
                models.Resource.note_id == note_model.id,
            )).delete(synchronize_session='fetch')        
        self.session.commit()
