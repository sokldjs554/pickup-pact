/* Unsubmitted controls belong to the visitor, not to a background GET. */
class PickupSelectionState {
  constructor(){this.sid=null;this.draft={};}
  begin(sid){if(this.sid!==sid){this.sid=sid;this.draft={};}}
  choose(key,value){this.draft[key]=value;}
  value(key,fallback){return Object.hasOwn(this.draft,key)?this.draft[key]:fallback;}
  applied(key,value){if(this.draft[key]===value)delete this.draft[key];}
}
globalThis.PickupSelectionState=PickupSelectionState;
