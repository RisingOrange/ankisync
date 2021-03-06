import peewee as pv
from playhouse import signals
import json
from time import time

from ankisync.builder.guid import guid64
from ankisync.builder.default import create_conf, create_tags
from ankisync.anki_util import field_checksum, stripHTMLMedia

database = pv.SqliteDatabase(None)


class BaseModel(signals.Model):
    class Meta:
        database = database


class TagField(pv.TextField):
    def db_value(self, value):
        return ' '.join(value)

    def python_value(self, value):
        return value.strip().split(' ')


class JSONField(pv.TextField):
    def db_value(self, value):
        all_names = set()

        for v2 in value.values():
            if isinstance(v2, dict):
                if 'name' in v2.keys():
                    if v2['name'] in all_names:
                        raise ValueError('Duplicate name: {}'.format(v2['name']))
                    all_names.add(v2['name'])

        return json.dumps(value)

    def python_value(self, value):
        return json.loads(value)


class ListField(pv.TextField):
    def db_value(self, value):
        if value:
            return '\u001f'.join(value)

    def python_value(self, value):
        return value.split('\u001f')


class Col(BaseModel):
    """
    -- col contains a single row that holds various information about the collection
    CREATE TABLE col (
        id              integer primary key,
          -- arbitrary number since there is only one row
        crt             integer not null,
          -- created timestamp
        mod             integer not null,
          -- last modified in milliseconds
        scm             integer not null,
          -- schema mod time: time when "schema" was modified.
          --   If server scm is different from the client scm a full-sync is required
        ver             integer not null,
          -- version
        dty             integer not null,
          -- dirty: unused, set to 0
        usn             integer not null,
          -- update sequence number: used for finding diffs when syncing.
          --   See usn in cards table for more details.
        ls              integer not null,
          -- "last sync time"
        conf            text not null,
          -- json object containing configuration options that are synced
        models          text not null,
          -- json array of json objects containing the models (aka Note types)
        decks           text not null,
          -- json array of json objects containing the deck
        dconf           text not null,
          -- json array of json objects containing the deck options
        tags            text not null
          -- a cache of tags used in the collection (This list is displayed in the browser. Potentially at other place)
    );
    """

    id = pv.IntegerField(primary_key=True, default=1)
    crt = pv.IntegerField(default=lambda: int(time()))
    mod = pv.IntegerField()         # autogenerated
    scm = pv.IntegerField(default=lambda: int(time() * 1000))
    ver = pv.IntegerField(default=11)
    dty = pv.IntegerField(default=0)
    usn = pv.IntegerField(default=0)
    ls = pv.IntegerField(default=0)
    conf = JSONField(default=create_conf)
    models = JSONField()
    decks = JSONField()
    dconf = JSONField()
    tags = JSONField(default=create_tags)


@signals.pre_save(sender=Col)
def col_pre_save(model_class, instance, created):
    instance.mod = int(time())


class Notes(BaseModel):
    """
    -- Notes contain the raw information that is formatted into a number of cards
    -- according to the models
    CREATE TABLE notes (
        id              integer primary key,
          -- epoch seconds of when the note was created
        guid            text not null,
          -- globally unique id, almost certainly used for syncing
        mid             integer not null,
          -- model id
        mod             integer not null,
          -- modification timestamp, epoch seconds
        usn             integer not null,
          -- update sequence number: for finding diffs when syncing.
          --   See the description in the cards table for more info
        tags            text not null,
          -- space-separated string of tags.
          --   includes space at the beginning and end, for LIKE "% tag %" queries
        flds            text not null,
          -- the values of the fields in this note. separated by 0x1f (31) character.
        sfld            text not null,
          -- sort field: used for quick sorting and duplicate check
        csum            integer not null,
          -- field checksum used for duplicate check.
          --   integer representation of first 8 digits of sha1 hash of the first field
        flags           integer not null,
          -- unused
        data            text not null
          -- unused
    );
    """
    id = pv.IntegerField(primary_key=True, default=lambda: int(time() * 1000))
    guid = pv.TextField(unique=True, default=guid64)    # autogenerated
    mid = pv.IntegerField()
    mod = pv.IntegerField()     # autogenerated
    usn = pv.IntegerField(default=-1)
    tags = TagField(default=list)
    flds = ListField()
    sfld = pv.TextField()       # autogenerated
    csum = pv.IntegerField()    # autogenerated
    flags = pv.IntegerField(default=0)
    data = pv.TextField(default='')

    class Meta:
        indexes = [
            pv.SQL('CREATE INDEX ix_notes_usn on notes (usn)'),
            pv.SQL('CREATE INDEX ix_notes_csum on notes (csum)')
        ]


@signals.pre_save(sender=Notes)
def notes_pre_save(model_class, instance, created):
    while model_class.get_or_none(id=instance.id) is not None:
        instance.id = model_class.select(pv.fn.Max(model_class.id)).scalar() + 1

    while model_class.get_or_none(guid=instance.guid) is not None:
        instance.guid = guid64()

    instance.mod = int(time() * 1000)
    instance.sfld = stripHTMLMedia(instance.flds[0])
    instance.csum = field_checksum(instance.sfld)


class Cards(BaseModel):
    """
    -- Cards are what you review.
    -- There can be multiple cards for each note, as determined by the Template.
    CREATE TABLE cards (
        id              integer primary key,
          -- the epoch milliseconds of when the card was created
        nid             integer not null,--
          -- notes.id
        did             integer not null,
          -- deck id (available in col table)
        ord             integer not null,
          -- ordinal : identifies which of the card templates it corresponds to
          --   valid values are from 0 to num templates - 1
        mod             integer not null,
          -- modificaton time as epoch seconds
        usn             integer not null,
          -- update sequence number : used to figure out diffs when syncing.
          --   value of -1 indicates changes that need to be pushed to server.
          --   usn < server usn indicates changes that need to be pulled from server.
        type            integer not null,
          -- 0=new, 1=learning, 2=due, 3=filtered
        queue           integer not null,
          -- -3=sched buried, -2=user buried, -1=suspended,
          -- 0=new, 1=learning, 2=due (as for type)
          -- 3=in learning, next rev in at least a day after the previous review
        due             integer not null,
         -- Due is used differently for different card types:
         --   new: note id or random int
         --   due: integer day, relative to the collection's creation time
         --   learning: integer timestamp
        ivl             integer not null,
          -- interval (used in SRS algorithm). Negative = seconds, positive = days
        factor          integer not null,
          -- factor (used in SRS algorithm)
        reps            integer not null,
          -- number of reviews
        lapses          integer not null,
          -- the number of times the card went from a "was answered correctly"
          --   to "was answered incorrectly" state
        left            integer not null,
          -- of the form a*1000+b, with:
          -- b the number of reps left till graduation
          -- a the number of reps left today
        odue            integer not null,
          -- original due: only used when the card is currently in filtered deck
        odid            integer not null,
          -- original did: only used when the card is currently in filtered deck
        flags           integer not null,
          -- currently unused
        data            text not null
          -- currently unused
    );
    """

    id = pv.IntegerField(primary_key=True, default=lambda: int(time() * 1000))
    nid = pv.IntegerField()
    did = pv.IntegerField()
    ord = pv.IntegerField()
    mod = pv.IntegerField()     # autogenerated
    usn = pv.IntegerField(default=-1)
    type = pv.IntegerField(default=0)
    queue = pv.IntegerField(default=0)
    due = pv.IntegerField()     # autogenerated
    ivl = pv.IntegerField(default=0)
    factor = pv.IntegerField(default=0)
    reps = pv.IntegerField(default=0)
    lapses = pv.IntegerField(default=0)
    left = pv.IntegerField(default=0)
    odue = pv.IntegerField(default=0)
    odid = pv.IntegerField(default=0)
    flags = pv.IntegerField(default=0)
    data = pv.TextField(default='')

    class Meta:
        indexes = [
            pv.SQL('CREATE INDEX ix_cards_usn on cards (usn)'),
            pv.SQL('CREATE INDEX ix_cards_nid on cards (nid)'),
            pv.SQL('CREATE INDEX ix_cards_sched on cards (did, queue, due)')
        ]


@signals.pre_save(sender=Cards)
def cards_pre_save(model_class, instance, created):
    while model_class.get_or_none(id=instance.id) is not None:
        instance.id = model_class.select(pv.fn.Max(model_class.id)).scalar() + 1

    instance.mod = int(time())
    if instance.due is None:
        instance.due = instance.nid


class Revlog(BaseModel):
    """
    -- revlog is a review history; it has a row for every review you've ever done!
    CREATE TABLE revlog (
        id              integer primary key,
           -- epoch-milliseconds timestamp of when you did the review
        cid             integer not null,
           -- cards.id
        usn             integer not null,
            -- update sequence number: for finding diffs when syncing.
            --   See the description in the cards table for more info
        ease            integer not null,
           -- which button you pushed to score your recall.
           -- review:  1(wrong), 2(hard), 3(ok), 4(easy)
           -- learn/relearn:   1(wrong), 2(ok), 3(easy)
        ivl             integer not null,
           -- interval
        lastIvl         integer not null,
           -- last interval
        factor          integer not null,
          -- factor
        time            integer not null,
           -- how many milliseconds your review took, up to 60000 (60s)
        type            integer not null
           --  0=learn, 1=review, 2=relearn, 3=cram
    );
    """

    id = pv.IntegerField(primary_key=True, default=lambda: int(time() * 1000))
    cid = pv.IntegerField()
    usn = pv.IntegerField(default=-1)
    ease = pv.IntegerField()
    ivl = pv.IntegerField()
    lastIvl = pv.IntegerField()
    factor = pv.IntegerField()
    time = pv.IntegerField()
    type = pv.IntegerField()

    class Meta:
        indexes = [
            pv.SQL('CREATE INDEX ix_revlog_usn on revlog (usn)'),
            pv.SQL('CREATE INDEX ix_revlog_cid on revlog (cid)')
        ]


@signals.pre_save(sender=Revlog)
def revlog_pre_save(model_class, instance, created):
    while model_class.get_or_none(id=instance.id) is not None:
        instance.id = model_class.select(pv.fn.Max(model_class.id)).scalar() + 1


class Graves(BaseModel):
    """
    -- Contains deleted cards, notes, and decks that need to be synced.
    -- usn should be set to -1,
    -- oid is the original id.
    -- type: 0 for a card, 1 for a note and 2 for a deck
    CREATE TABLE graves (
        usn             integer not null,
        oid             integer not null,
        type            integer not null
    );
    """

    usn = pv.IntegerField(default=-1)
    oid = pv.IntegerField()
    type = pv.IntegerField()

    class Meta:
        primary_key = False
