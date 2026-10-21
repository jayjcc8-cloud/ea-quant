import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {afterEach,expect,it,vi} from 'vitest'
import {useState} from 'react'
import {ResearchComposer} from './research-composer'

afterEach(cleanup)
function mount(locked=false) {
  const send=vi.fn(), choose=vi.fn(),stop=vi.fn()
  function Harness(){const[value,setValue]=useState('');return <ResearchComposer value={value} change={setValue} send={send}
    sending={false} canSend={!!value} locked={locked} status="草稿 v1" stop={locked?stop:undefined}
    commands={[{name:'model',label:'模型连接',scope:'本研究 · 下一轮生效',options:[{value:'deepseek',label:'DeepSeek'},{value:'local',label:'Local'}],choose}]} />}
  render(<Harness/>);return{send,choose,stop,input:screen.getByLabelText('消息')}
}
it('selects real slash values by arrows and Enter without sending a model request',async()=>{
  const user=userEvent.setup();const{input,send,choose}=mount()
  await user.type(input,'/');await user.keyboard('{Enter}')
  expect((input as HTMLTextAreaElement).value).toBe('/model ')
  await user.keyboard('{ArrowDown}{Enter}')
  expect(choose).toHaveBeenCalledWith('local');expect(send).not.toHaveBeenCalled()
  expect((input as HTMLTextAreaElement).value).toBe('')
})
it('searches configuration and Escape never turns a slash command into a prompt',async()=>{
  const user=userEvent.setup();const{input,send,choose}=mount()
  await user.type(input,'/model Deep');expect(screen.getAllByRole('option')).toHaveLength(1)
  await user.keyboard('{Escape}{Enter}');expect(send).not.toHaveBeenCalled();expect(choose).not.toHaveBeenCalled()
  expect(screen.queryByRole('listbox')).toBeNull()
})
it('does not select or send during Chinese composition or legacy keyCode 229',()=>{
  const{input,send,choose}=mount()
  fireEvent.change(input,{target:{value:'/model '}});fireEvent.compositionStart(input)
  fireEvent.keyDown(input,{key:'Enter'});expect(choose).not.toHaveBeenCalled()
  fireEvent.compositionEnd(input);fireEvent.change(input,{target:{value:'研究'}})
  fireEvent.keyDown(input,{key:'Enter',isComposing:true});fireEvent.keyDown(input,{key:'Enter',keyCode:229})
  expect(send).not.toHaveBeenCalled()
})
it('locks configuration during execution while keeping stop available',async()=>{
  const user=userEvent.setup();const{input,choose,stop}=mount(true)
  await user.type(input,'/model ');await user.keyboard('{ArrowDown}{Enter}')
  expect(choose).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button',{name:'停止生成'}));expect(stop).toHaveBeenCalledOnce()
})

it('keeps Shift+Enter as a newline while a command menu is open',async()=>{
  const user=userEvent.setup();const{input,send,choose}=mount()
  await user.type(input,'/model ')
  await user.keyboard('{Shift>}{Enter}{/Shift}')
  expect((input as HTMLTextAreaElement).value).toBe('/model \n')
  expect(choose).not.toHaveBeenCalled();expect(send).not.toHaveBeenCalled()
})


it('preserves an unsent multiline draft when the configuration button is cancelled',async()=>{
  const user=userEvent.setup();const{input,send,choose}=mount()
  const draft='保留草稿\nUNSENT'
  fireEvent.change(input,{target:{value:draft}})
  await user.click(screen.getByRole('button',{name:'打开配置菜单'}))
  await user.keyboard('{Escape}')
  expect((input as HTMLTextAreaElement).value).toBe(draft)
  expect(send).not.toHaveBeenCalled();expect(choose).not.toHaveBeenCalled()
})

it('keeps the host draft untouched while a button-opened configuration is chosen',async()=>{
  const user=userEvent.setup();const send=vi.fn(),change=vi.fn(),choose=vi.fn()
  const draft='仍未发送的研究消息'
  render(<ResearchComposer value={draft} change={change} send={send} sending={false} canSend locked={false} status="草稿"
    commands={[{name:'model',label:'模型',scope:'本研究',options:[{value:'local',label:'Local'}],choose}]} />)
  await user.click(screen.getByRole('button',{name:'打开配置菜单'}))
  await user.keyboard('{Enter}{Enter}')
  expect(choose).toHaveBeenCalledWith('local')
  expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe(draft)
  expect(change).not.toHaveBeenCalled();expect(send).not.toHaveBeenCalled()
})
