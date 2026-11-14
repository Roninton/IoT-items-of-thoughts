// Reuse the archive's verified private filesystem/locking, never change its event log.
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { SessionArchive, archiveFiles, archiveSourceLink } from './session-archive.mjs';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const fail = (code, text) => { throw Object.assign(new Error(text), {code}); };
const validId = id => id === 'summary' || typeof id === 'string' && UUID.test(id);
function page(args) {
  const offset = args.offset ?? 0, limit = args.limit ?? 20;
  if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 100) fail('MEMORY_PAGE', 'offset >= 0; limit 1..100');
  return {offset,limit};
}
const safeHeading = text => String(text).replace(/[\r\n]/g,' ').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
export function renderSessionMemory(sessionId, history) {
  const current = new Map(history.map(item => [item.id, item]));
  const render = item => {
    const links = (item.sources ?? []).map(source => `[第 ${source.turn} 轮${source.seq === undefined ? '' : ` · seq ${source.seq}`}](<${archiveSourceLink(sessionId, source)}>)`);
    return `### ${safeHeading(item.title)}\n\nID: ${item.id} · revision ${item.revision} · 更新：${item.updatedAt}（UTC）\n\n${item.content}\n\n来源：${links.join(' · ') || '未记录'}\n`;
  };
  const overview = current.get('summary');
  return ['# Session 记忆', '', '> 模型维护的工作记录，不是用户授权，也不自动等于已验证事实。', '', '[会话目录](<index.md>)', '', `Session: ${sessionId}`, '', '## 当前状态', overview && !overview.deleted ? render(overview) : '（尚未记录）', '', '## 关键记忆', ...[...current.values()].filter(x=>!x.deleted&&x.id!=='summary').map(render)].join('\n')+'\n';
}
export class SessionMemory {
  constructor({root}) { this.archive = new SessionArchive({root}); }
  execute(sessionId, args = {}) {
    const action = args.action ?? 'read';
    if (!['list','read','search','write','update','delete'].includes(action)) fail('MEMORY_ACTION', 'Unknown action');
    const mutation = ['write','update','delete'].includes(action);
    return this.archive._locked(sessionId, true, dir => {
      const file = join(dir, 'memories.json');
      const state = archiveFiles.exists(file) ? JSON.parse(archiveFiles.read(file)) : {version:1,sessionId,history:[]};
      if (state.version !== 1 || state.sessionId !== sessionId || !Array.isArray(state.history)) fail('MEMORY_CORRUPT','Invalid memory store');
      const current = new Map();
      for (const item of state.history) {
        if (!validId(item.id) || !Number.isSafeInteger(item.revision) || item.revision !== (current.get(item.id)?.revision ?? 0)+1) fail('MEMORY_CORRUPT','Invalid memory revisions');
        current.set(item.id,item);
      }
      const id = args.id ?? (action === 'write' ? randomUUID() : 'summary');
      if (!validId(id)) fail('MEMORY_ID','id must be summary or a UUID');
      const previous = current.get(id);
      if (!mutation) {
        if (action === 'read') {
          if (!previous || previous.deleted) return {ok:true,found:false,id};
          return {ok:true,found:true,memory:previous,directory:dir};
        }
        const {offset,limit}=page(args);
        if (action==='search' && (typeof args.query!=='string'||!args.query.trim()||args.query.length>200)) fail('MEMORY_QUERY','query requires 1..200 characters');
        const query = (args.query ?? '').toLowerCase();
        const matches=[...current.values()].filter(x=>!x.deleted && (action!=='search'||`${x.title}\n${x.content}`.toLowerCase().includes(query))).sort((a,b)=>b.updatedAt.localeCompare(a.updatedAt));
        return {ok:true,total:matches.length,offset,items:matches.slice(offset,offset+limit).map(({content,...item})=>({...item,excerpt:content.slice(0,200)}))};
      }
      if(action==='write' && previous) fail('MEMORY_EXISTS','ID already exists; use update with expectedRevision');
      if(action!=='write') {
        if(!previous || previous.deleted) fail('MEMORY_NOT_FOUND','Memory not found');
        if(args.expectedRevision!==previous.revision) fail('MEMORY_CONFLICT','Read latest revision before update/delete');
      }
      const title=args.title ?? previous?.title;
      const content=args.content ?? previous?.content;
      if(action!=='delete') {
        if(typeof title!=='string'||!title.trim()||title.length>160) fail('MEMORY_TITLE','title requires 1..160 characters');
        if(typeof content!=='string'||!content.trim()||content.length>(id==='summary'?8000:32000)) fail('MEMORY_CONTENT','content requires 1..8000 (summary) or 1..32000 characters');
      }
      const sources=args.sources ?? previous?.sources ?? [];
      if(!Array.isArray(sources)||sources.length>50||sources.some(x=>!x||!Number.isSafeInteger(x.turn)||x.turn<0||(x.seq!==undefined&&(!Number.isSafeInteger(x.seq)||x.seq<0)))) fail('MEMORY_SOURCES','sources require turn >= 0 and optional seq >= 0');
      const item={id,revision:(previous?.revision??0)+1,title,content,sources:sources.map(x=>({turn:x.turn,...x.seq===undefined?{}:{seq:x.seq}})),deleted:action==='delete',updatedAt:new Date().toISOString(),source:'model-authored-session-memory'};
      state.history.push(item);
      archiveFiles.atomic(file,JSON.stringify(state,null,2)+'\n');
      current.set(id,item);
      const text=renderSessionMemory(sessionId,state.history);
      let rendered=true;
      try{archiveFiles.atomic(join(dir,'memory.md'),text+'\n');}catch{rendered=false;}
      return {ok:true,id,revision:item.revision,deleted:item.deleted,rendered,directory:dir,note:action==='delete'?'Logical deletion; revision history and original logs remain.':undefined};
    });
  }
}
