'use client';
import {useEffect,useState} from 'react';
export default function Page() {
 const [state,setState]=useState<any>(null);
 async function load(){setState(await (await fetch('/api/state')).json())}
 useEffect(()=>{load()},[]);
 async function act(name:string){await fetch('/api/'+name,{method:'POST'});await load()}
 return <main><h1>Runtime acceptance fixture</h1>
  <div id="state" data-loaded={!!state} data-project={state?.project_id||''} data-task={state?.task_id||''}>
   {!state?'Loading':!state.project_id?<button id="create" onClick={()=>act('create')}>Create project</button>:<>
    <p id="project">{state.project_id}</p>
    {!state.ready?<p id="blocked">capability reference mismatch</p>:<button id="generate" onClick={()=>act('submit')}>Generate once</button>}
    {state.task_id&&<p id="done">{state.task_id}</p>}
   </>}
  </div></main>
}
