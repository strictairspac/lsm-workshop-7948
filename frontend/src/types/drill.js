/** @typedef {{id:number,name:string,description:string,dataset_id:?number,updated_at:string,transaction_count:number,commit_seq:number,segment_count:number}} DrillSessionListItem */
/** @typedef {{version:number,value:?string,deleted:boolean}} DrillRecord */
/** @typedef {{id:number,kind:string,records:Object<string,DrillRecord>}} DrillSegment */
/** @typedef {{op:string,value:?string,base_version:number}} DrillStagedIntent */
/** @typedef {{id:string,label:string,state:string,snapshot:Object,read_ranges:Array,read_predicates:Object,staged:Object<string,DrillStagedIntent>,result:?Object,started_seq:number,ended_seq:?number}} DrillTxn */
/** @typedef {{id:number,name:string,description:string,dataset_id:?number,commit_seq:number,live_keyspace:Object<string,string>,segments:Array<DrillSegment>,events:Array,transactions:Object<string,DrillTxn>}} DrillSessionDetail */
export const drillEntityNames={drillSession:'事务演练场',transaction:'乐观事务'};
