"""Bounded, event-scoped Solver arbitration and shared native frame ownership."""
import threading
import time
import uuid


class SolverRecovery:
    def __init__(self):
        self.lock=threading.RLock()
        self.history={}
        self.exhausted=set()
        self.event=None
        self.frame=None
        self.frame_id=None
        self.device=None
        self.backend=None
        self.before=None
        self.attempted=[]
        self.examined=[]

    def begin(self, problem, candidates, guarded=()):
        with self.lock:
            identity=str(problem or 'unspecified')
            attempted=self.history.setdefault(identity,[])
            attempted[:]=[task for task in attempted if task in candidates]
            # Keep the next-rank policy within a pass, but never blacklist all
            # Solvers permanently. A new failure event may start a fresh pass;
            # eligible()/poll() must not restart one inside the same event.
            restarted=bool(candidates) and (identity in self.exhausted or
                                           all(task in attempted for task in candidates))
            if restarted:
                attempted.clear()
            self.exhausted.discard(identity)
            self.history={key:value for key,value in list(self.history.items())[-64:]}
            self.exhausted.intersection_update(self.history)
            guarded=set(guarded)
            self.event={'id':uuid.uuid4().hex,'problem':identity,'candidates':list(candidates),
                        'guards':{task:'PENDING' for task in candidates if task in guarded and task not in attempted},
                        'status':'OBSERVING','selected':None,'results':[],
                        'cycle_restarted':restarted}
            self.attempted=attempted
            self.examined=[]
            self.frame=self.frame_id=self.device=self.backend=self.before=None
            return self.event['id']

    def probe_eligible(self, task):
        with self.lock:
            return bool(self.event and self.event['status']=='OBSERVING'
                        and task not in self.attempted and task not in self.examined
                        and self.event['guards'].get(task)=='PENDING')

    def guard_decision(self, task, allowed):
        with self.lock:
            if self.probe_eligible(task):
                self.event['guards'][task]='MATCHED' if allowed is True else 'REJECTED'

    def eligible(self, task):
        with self.lock:
            if not self.event or self.event['status'] not in {'OBSERVING','OPERATING'}:
                return False
            if self.event['selected']:
                return task==self.event['selected']
            if 'PENDING' in self.event['guards'].values():
                return False
            remaining=[item for item in self.event['candidates']
                       if item not in self.attempted and item not in self.examined
                       and self.event['guards'].get(item,'MATCHED')=='MATCHED']
            # A matched Guard takes precedence over an unconditional fallback.
            remaining.sort(key=lambda item:item not in self.event['guards'])
            if not remaining:
                self.event['status']='EXHAUSTED'
                self.exhausted.add(self.event['problem'])
                return False
            return task==remaining[0]

    def select(self, task):
        with self.lock:
            if not self.eligible(task):
                return False
            if task not in self.attempted:self.attempted.append(task)
            self.event.update(selected=task,status='OPERATING')
            return True

    def reject(self, task):
        with self.lock:
            if not self.event:return
            if self.event['guards'].get(task)=='PENDING':
                self.event['guards'][task]='REJECTED'
            if task not in self.examined:self.examined.append(task)
            if self.event['selected']==task:self.event['selected']=None
            if self.event['status'] in {'OBSERVING','OPERATING'}:
                self.event['status']='OBSERVING'

    def unused(self, task):
        """A reserved candidate whose guarded operation never ran used no rank."""
        with self.lock:
            if task in self.attempted:self.attempted.remove(task)

    @staticmethod
    def describe(frame, frame_id):
        import numpy as np
        if not isinstance(frame,np.ndarray) or frame.ndim not in (2,3) or not frame.size:
            return {'valid':False,'frame_id':frame_id,'captured_at':time.time()}
        pixels=frame[::8,::8]
        spatial_std=(float(np.std(pixels)) if pixels.ndim==2
                     else float(np.std(pixels.astype('float32'),axis=(0,1)).max()))
        if spatial_std<1:
            return {'valid':False,'frame_id':frame_id,'captured_at':time.time()}
        # Keep only a small uncompressed sample for comparison, never full frames in logs.
        ys=np.linspace(0,frame.shape[0]-1,32,dtype=int)
        xs=np.linspace(0,frame.shape[1]-1,32,dtype=int)
        sample=frame[ys[:,None],xs].astype('float32')
        return {'valid':True,'frame_id':frame_id,'captured_at':time.time(),
                'shape':list(frame.shape),'sample':sample.tolist()}

    def shared_frame(self, backend, device, validate):
        with self.lock:
            if self.frame is None:
                frame=backend.get_frame()
                validate(frame)
                frame_id=uuid.uuid4().hex
                info=self.describe(frame,frame_id)
                if not info['valid']:raise ValueError('Solver baseline frame invalid/uniform')
                self.frame,self.frame_id,self.device,self.backend=frame,frame_id,dict(device),backend
                self.before=info
            if self.device!=device or self.backend is not backend:
                raise ValueError('Solver shared frame device changed')
            return self.frame,self.frame_id

    def finish(self, task, after):
        with self.lock:
            before=self.before
            valid=bool(before and before.get('valid') and after.get('valid')
                       and before.get('shape')==after.get('shape'))
            changed=None
            delta=None
            if valid:
                import numpy as np
                difference=np.abs(np.asarray(before['sample'])-np.asarray(after['sample']))/255
                delta=float(difference.mean())
                changed=bool(delta>=.02 and float((difference>=.05).mean())>=.05)
            record={'solver':task,'event':self.event['id'],'valid':valid,'changed':changed,
                    'mean_difference':delta,
                    'before':{k:v for k,v in (before or {}).items() if k!='sample'},
                    'after':{k:v for k,v in after.items() if k!='sample'}}
            self.event['results'].append(record)
            self.event['results']=self.event['results'][-100:]
            self.event.update(status='SOLVED' if changed else 'OBSERVING',selected=None)
            if task not in self.examined:self.examined.append(task)
            if changed:
                self.frame=None  # Release pixels; preserve rank for a recurrence of this problem.
            return record
