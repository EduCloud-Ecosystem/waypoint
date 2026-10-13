"""Server-owned reviewed plan references; no client filesystem paths."""
from contextlib import contextmanager
from pathlib import Path
import uuid

import ownership_contract as ownership
from authority_access import AuthorityError, private, read_private, HEX32, HEX64


class CheckedConnection:
    def __init__(self,connection,authorize):self.connection=connection;self.authorize=authorize
    def __getattr__(self,name):return getattr(self.connection,name)
    def execute(self,statement,*args):
        verb=statement.lstrip().upper()
        if verb.startswith('BEGIN IMMEDIATE'):
            value=self.connection.execute(statement,*args);self.authorize();return value
        if verb.startswith(('INSERT','UPDATE','DELETE')):self.authorize()
        return self.connection.execute(statement,*args)


class ScopedBackend:
    def __init__(self,backend,principal,course):self.backend=backend;self.principal=principal;self.course=course
    def __getattr__(self,name):return getattr(self.backend,name)
    def authorize(self,request,core_authorize):
        self.principal['authorize']()
        if (request['course_id']!=self.course or any(request[key] not in self.principal['workers'] for key in ('source_worker_id','receiver_worker_id'))):raise AuthorityError('authority_access_refused')
        core_authorize();self.principal['authorize']()
    @contextmanager
    def fence_context(self,request,*,authorize):
        with self.backend.fence_context(request,authorize=lambda:self.authorize(request,authorize)) as context:yield context
    @contextmanager
    def reconciliation_context(self,request,*,authorize):
        with self.backend.reconciliation_context(request,authorize=lambda:self.authorize(request,authorize)) as context:yield context


class ScopedAuthority(ownership.Authority):
    def __init__(self,directory,authority_id,backend,principal):
        super().__init__(directory,authority_id,backend);self.principal=principal
    @contextmanager
    def _connection(self):
        with super()._connection() as connection:yield CheckedConnection(connection,self.principal['authorize'])


class OperatorDispatcher:
    def __init__(self,authority,plan_directory):
        self.authority=authority;self.directory=Path(plan_directory);private(self.directory,True)
        if self.directory==authority.directory or authority.directory in self.directory.parents or self.directory in authority.directory.parents:raise AuthorityError('authority_access_refused')

    def _scope(self,principal,course,workers):
        principal['authorize']()
        if (principal['role']!='operator' or principal['course_id']!=course or course not in principal['courses']
                or principal['worker_id'] not in workers or any(worker not in principal['workers'] for worker in workers)):
            raise AuthorityError('authority_access_refused')

    def dispatch(self,principal,operation,course,payload):
        try:
            principal['authorize']()
            if principal['role']!='operator' or course!=principal['course_id'] or course not in principal['courses']:raise AuthorityError('authority_access_refused')
            scoped=ScopedAuthority(self.authority.directory,self.authority.authority_id,ScopedBackend(self.authority.backend,principal,course),principal)
            if operation in ('ownership.preview','ownership.reconcile_preview'):
                field='receiver_worker_id' if operation=='ownership.preview' else 'selected_worker_id'
                if not isinstance(payload,dict) or set(payload)!={field}:raise AuthorityError('authority_frame_invalid')
                with scoped._connection() as connection:
                    connection.execute('BEGIN');scoped._validate(connection);current=scoped._course(connection,course)
                    if operation=='ownership.preview':workers={current['owner'],payload[field]}
                    else:
                        row=connection.execute('SELECT plan FROM transfers WHERE transaction_id=?',(current['pending'],)).fetchone()
                        if row is None:raise AuthorityError('authority_operation_refused')
                        workers=set(ownership.parsed(row['plan'])['workers'])
                        if payload[field] not in workers:raise AuthorityError('authority_access_refused')
                    self._scope(principal,course,workers);connection.execute('COMMIT')
                plan_id=uuid.uuid4().hex;path=self.directory/(plan_id+'.json')
                result=(scoped.preview(course,payload[field],path) if operation=='ownership.preview' else scoped.reconcile_preview(course,payload[field],path))
                principal['authorize']();result['plan_id']=plan_id;return result
            if operation in ('ownership.apply','ownership.reconcile_apply'):
                if (not isinstance(payload,dict) or set(payload)!={'plan_id','plan_sha256'} or not isinstance(payload['plan_id'],str) or not HEX32.fullmatch(payload['plan_id'])
                        or not isinstance(payload['plan_sha256'],str) or not HEX64.fullmatch(payload['plan_sha256'])):raise AuthorityError('authority_frame_invalid')
                path=self.directory/(payload['plan_id']+'.json');plan=read_private(path)
                if plan.get('digest')!=payload['plan_sha256'] or plan.get('course_id')!=course:raise AuthorityError('authority_access_refused')
                if operation=='ownership.apply' and plan.get('version')!=ownership.VERSION or operation=='ownership.reconcile_apply' and plan.get('version')!=ownership.RECONCILIATION_VERSION:raise AuthorityError('authority_frame_invalid')
                self._scope(principal,course,set(plan['workers']))
                return scoped.apply(path) if operation=='ownership.apply' else scoped.reconcile_apply(path)
            if operation=='ownership.inventory':
                if payload:raise AuthorityError('authority_frame_invalid')
                with scoped._connection() as connection:
                    connection.execute('BEGIN');scoped._validate(connection);current=scoped._course(connection,course)
                    self._scope(principal,course,{current['owner'],principal['worker_id']})
                    result=scoped._summary('INVENTORY',current);result.update(course_id=course,owner_worker_id=current['owner'],pending=current['pending'] is not None)
                    result['reservations']={row['worker_id']:{'status':row['status'],'profile':ownership.parsed(row['profile'])}
                        for row in connection.execute('SELECT * FROM reservations WHERE course_id=?',(course,)) if row['worker_id'] in principal['workers']}
                    connection.execute('COMMIT');return result
            raise AuthorityError('authority_access_refused')
        except AuthorityError:raise
        except Exception:raise AuthorityError('authority_operation_refused') from None
