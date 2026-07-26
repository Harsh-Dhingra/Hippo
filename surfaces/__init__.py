"""Ways to reach Hippo that are not the REST API.

ARCHITECTURE section 2 puts auth, sessions and approval buttons in the API and
no business logic. A surface is one step further out: a protocol adapter with
none at all. Every one of these is a client of api/routes.py, holds no database
credential, and can reach content only through a bearer token belonging to one
person.

That is what makes adding a surface cheap and safe. The permission story lives
in the database and in one SQL function; a surface cannot widen it, because it
has nothing to widen it with.
"""
