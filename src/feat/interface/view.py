# F3AT - Flumotion Asynchronous Autonomous Agent Toolkit
# Copyright (C) 2010,2011 Flumotion Services, S.A.
# All rights reserved.

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

# See "LICENSE.GPL" in the source distribution for more information.

# Headers in this file shall remain intact.
from zope.interface import Interface, Attribute

__all__ = ["DESIGN_DOC_ID", "IViewFactory"]


DESIGN_DOC_ID = u'feat'


class IViewFactory(Interface):
    '''
    Interface implemented by a view class. It exposes methods getting data
    about necessary for building design document and to parse the result
    of the query.
    '''

    name = Attribute('C{unicode}. Unique name of the view')
    use_reduce = Attribute('C{bool}. Should the reduce function be used')

    def map(doc):
        '''
        Function called for every document in the database.
        It has to be a generator yielding tuples (key, value).
        @param doc: document in couchdb
        @type doc: C{dict}. It always has _id and _rev keys. The rest is
                   specific to the application.
        '''

    def reduce(keys, values):
        '''
        Defined optionaly if use_reduce = True.
        Function called with the list of results emited by the map() calls
        for all the documents. It should return a result calculated for
        everything.

        @param keys: Keys generated for the documents being reduced.
        @param values: Values generated for the documents being reduced.
        @return: Resulting value.
        '''

    def parse(key, value, reduced):
        '''
        Map the (key, value) pair to the python object of our choice.
        @param reduced: Flag telling if we are parsing the map function
                        result or the reduced data. Usefull for creating
                        views which works both ways.
        @return: Any instance.
        '''
