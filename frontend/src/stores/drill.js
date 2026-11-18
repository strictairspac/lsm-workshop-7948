import{listSessions,sessionDetail}from'../api/drill.js';
export const drill={
  sessions:[],
  current:null,
  async loadSessions(){this.sessions=await listSessions();return this.sessions},
  async open(id){this.current=await sessionDetail(id);return this.current},
  async refresh(){if(this.current)return this.open(this.current.id)}
};
