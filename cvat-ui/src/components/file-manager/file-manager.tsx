// Copyright (C) 2020 Intel Corporation
//
// SPDX-License-Identifier: MIT

import './styles.scss';
import React from 'react';
import Tabs from 'antd/lib/tabs';
import Icon from 'antd/lib/icon';
import Input from 'antd/lib/input';
import Text from 'antd/lib/typography/Text';
import Paragraph from 'antd/lib/typography/Paragraph';
import Upload, { RcFile } from 'antd/lib/upload';
import Empty from 'antd/lib/empty';
import Tree, { AntTreeNode, TreeNodeNormal } from 'antd/lib/tree/Tree';
import getCore from 'cvat-core-wrapper';
const cvat = getCore();

import consts from 'consts';
import { Alert } from 'antd';

export interface Files {
    local: File[];
    share: string[];
    remote: string[];
}

interface State {
    loadedKeys: string[],
    files: Files;
    expandedKeys: string[];
    active: 'local' | 'share' | 'remote';
    status?: any;
}

interface Props {
    withRemote: boolean;
    treeData: TreeNodeNormal[];
    onLoadData: (key: string, success: () => void, failure: () => void) => void;
}

export default class FileManager extends React.PureComponent<Props, State> {
    intervalID: any;

    public constructor(props: Props) {
        super(props);

        this.state = {
            files: {
                local: [],
                share: [],
                remote: [],
            },
            loadedKeys: [],
            expandedKeys: [],
            active: 'local',
        };

        this.loadData('/');
    }

    public getFiles(): Files {
        const {
            active,
            files,
        } = this.state;
        return {
            local: active === 'local' ? files.local : [],
            share: active === 'share' ? files.share : [],
            remote: active === 'remote' ? files.remote : [],
        };
    }

    private loadData = (key: string): Promise<void> => new Promise<void>(
        (resolve, reject): void => {
            const { onLoadData } = this.props;

            const success = (): void => resolve();
            const failure = (): void => reject();
            onLoadData(key, success, failure);

            // Only do this if mounted. Otherwise it's an error.
            if(this.intervalID) {
                const { loadedKeys } = this.state;
                loadedKeys.push(key);
                this.setState({
                    loadedKeys
                });
            }
        },
    );

    public reset(): void {
        this.setState({
            loadedKeys: [],
            expandedKeys: [],
            active: 'local',
            files: {
                local: [],
                share: [],
                remote: [],
            },
        });
    }
    
    
    getFileSyncerStatus(){
        const baseUrl: string = cvat.config.backendAPI.slice(0, -7);
        const timestamp = +new Date();
        // replace url with baseURL in docker image
        fetch(baseUrl + '/sys/filesyncer/api/status?tsp=' + timestamp)
            .then(response=>response.json())
            .then(data=>{
                const { status } = this.state;

                if(status) {
                    data['wasDownloading'] = status['isDownloading'];
                    data['wasUploading'] = status['isUploading'];

                    if(data['wasDownloading'] && !data['isDownloading']) {
                        data['refreshRequired'] = true;
                    }

                    if(!status['lastDownload'] && data['lastDownload']) {
                        data['refreshRequired'] = true;
                    } else if(status['lastDownload'] && data['lastDownload']) {
                        const oldDownload = (new Date(status['lastDownload'])).getTime();
                        const newDownload = (new Date(data['lastDownload'])).getTime();

                        if( (newDownload - oldDownload) > 0) {
                            data['refreshRequired'] = true;
                        }
                    }

                    if(status['refreshRequired']) {
                        data['refreshRequired'] = true;
                    }
                }

                this.setState({status:data});
            });
      
    }

    componentDidMount() {
        this.getFileSyncerStatus();
        this.intervalID = setInterval(()=>this.getFileSyncerStatus(), 5000);      
    }

    componentWillUnmount() {
        /*
            stop getData() from continuing to run even
            after unmounting this component. Notice we are calling
            'clearTimeout()` here rather than `clearInterval()` as
            in the previous example.
        */
        if(this.intervalID) {
            clearInterval(this.intervalID);
        }
    }


    private renderLocalSelector(): JSX.Element {
        const { files } = this.state;

        return (
            <Tabs.TabPane key='local' tab='My computer'>
                <Upload.Dragger
                    multiple
                    listType='text'
                    fileList={files.local as any[]}
                    showUploadList={files.local.length < 5 && {
                        showRemoveIcon: false,
                    }}
                    beforeUpload={(_: RcFile, newLocalFiles: RcFile[]): boolean => {
                        this.setState({
                            files: {
                                ...files,
                                local: newLocalFiles,
                            },
                        });
                        return false;
                    }}
                >
                    <p className='ant-upload-drag-icon'>
                        <Icon type='inbox' />
                    </p>
                    <p className='ant-upload-text'>Click or drag files to this area</p>
                    <p className='ant-upload-hint'>
                        Support for a bulk images or a single video
                    </p>
                </Upload.Dragger>
                { files.local.length >= 5
                    && (
                        <>
                            <br />
                            <Text className='cvat-text-color'>
                                {`${files.local.length} files selected`}
                            </Text>
                        </>
                    )}
            </Tabs.TabPane>
        );
    }

    private refreshFiles() {
        const { files, status } = this.state;

        delete status['refreshRequired'];

        this.setState({
            loadedKeys: [],
            expandedKeys: [],
            files: {
                ...files,
                share: [],
            },
            status: status
        });  
    }

    
    private renderFileSyncerDownloadedMsg(status: any){
        if(!status) {
            return;
        }

        if(status.refreshRequired){
            return (
                <div className="ant-alert ant-alert-info ant-alert-no-icon">
                    <span>All files are synced from object storage.
                        <a style={{marginLeft: '5px'}} onClick={() => this.refreshFiles()}>Refresh</a>
                    </span>
                </div>
            )
        } 
        
        if(status.error) {
            const errorMessage = `Error downloading files. ${status.error}.`;
            return (
                <Alert 
                    message={errorMessage}
                    type="error"/>
            )  
        } else if(status.isDownloading) {
            return (
                <Alert 
                    message="Syncing new files from object storage..."
                    type="info"/>
            )
        }
    }
    
    private renderShareSelector(): JSX.Element {
        function renderTreeNodes(data: TreeNodeNormal[]): JSX.Element[] {
            return data.map((item: TreeNodeNormal) => {
                if (item.children) {
                    return (
                        <Tree.TreeNode
                            title={item.title}
                            key={item.key}
                            dataRef={item}
                            isLeaf={item.isLeaf}
                        >
                            {renderTreeNodes(item.children)}
                        </Tree.TreeNode>
                    );
                }

                return <Tree.TreeNode key={item.key} {...item} dataRef={item} />;
            });
        }

        const { SHARE_MOUNT_GUIDE_URL } = consts;
        const { treeData } = this.props;
        const {
            expandedKeys,
            files,
            status,
            loadedKeys
        } = this.state;

        return (
            <Tabs.TabPane key='share' tab='Connected file share'>
                <div className="ant-text">
                    {this.renderFileSyncerDownloadedMsg(status)}
                </div>
                { treeData[0].children && treeData[0].children.length
                    ? 
                    (
                        <Tree
                            className='cvat-share-tree'
                            checkable
                            showLine
                            checkStrictly={false}
                            expandedKeys={expandedKeys}
                            checkedKeys={files.share}
                            loadedKeys={loadedKeys}
                            loadData={(node: AntTreeNode): Promise<void> => 
                                this.loadData(node.props.dataRef.key) 
                            }
                            onExpand={(newExpandedKeys: string[]): void => {
                                this.setState({
                                    expandedKeys: newExpandedKeys,
                                });
                            }}
                            onCheck={
                                (checkedKeys: string[] | {
                                    checked: string[];
                                    halfChecked: string[];
                                }): void => {
                                    const keys = checkedKeys as string[];
                                    this.setState({
                                        files: {
                                            ...files,
                                            share: keys,
                                        },
                                    });
                                }
                            }
                        >
                            { renderTreeNodes(treeData) }
                        </Tree>
                    ) : (
                        <div className='cvat-empty-share-tree'>
                            <Empty />
                            <Paragraph className='cvat-text-color'>
                                Please, be sure you had
                                <Text strong>
                                    <a href={SHARE_MOUNT_GUIDE_URL}> mounted </a>
                                </Text>
                                share before you built CVAT and the shared storage contains files
                            </Paragraph>
                        </div>
                    )}
            </Tabs.TabPane>
        );
    }

    private renderRemoteSelector(): JSX.Element {
        const { files } = this.state;

        return (
            <Tabs.TabPane key='remote' tab='Remote sources'>
                <Input.TextArea
                    placeholder='Enter one URL per line'
                    rows={6}
                    value={[...files.remote].join('\n')}
                    onChange={(event: React.ChangeEvent<HTMLTextAreaElement>): void => {
                        this.setState({
                            files: {
                                ...files,
                                remote: event.target.value.split('\n'),
                            },
                        });
                    }}
                />
            </Tabs.TabPane>
        );
    }

    public render(): JSX.Element {
        const { withRemote } = this.props;
        const { active } = this.state;

        return (
            <>
                <Tabs
                    type='card'
                    activeKey={active}
                    tabBarGutter={5}
                    onChange={
                        (activeKey: string): void => this.setState({
                            active: activeKey as any,
                        })
                    }
                >
                    { this.renderLocalSelector() }
                    { this.renderShareSelector() }
                    { withRemote && this.renderRemoteSelector() }
                </Tabs>
            </>
        );
    }
}