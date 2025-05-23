import os
import webbrowser
import shutil
import json
import requests
import re
import wx
import time
import tempfile
from threading import Thread
from .result_event import *
from .config import *


class PushThread(Thread):
    def __init__(self, wxObject):
        Thread.__init__(self)
        self.wxObject = wxObject
        self.start()

    def run(self):
        temp_dir = tempfile.mkdtemp()
        fd, temp_file = tempfile.mkstemp()
        # close temporary created file to be able to delete it later
        os.close(fd)
        
        board = pcbnew.GetBoard()
        title_block = board.GetTitleBlock()
        self.report(10)
        match = re.match(
            '^AISLER Project ID: ([A-Z]{8})$',
            title_block.GetComment(commentLineIdx))
        if match:
            project_id = match.group(1)
        else:
            project_id = False

        # Override a few design parameters as our CAM takes care of this
        settings = board.GetDesignSettings()
        settings.m_SolderMaskMargin = 0
        settings.m_SolderMaskMinWidth = 0

        pctl = pcbnew.PLOT_CONTROLLER(board)

        popt = pctl.GetPlotOptions()
        popt.SetOutputDirectory(temp_dir)
        popt.SetPlotFrameRef(False)
        popt.SetSketchPadLineWidth(pcbnew.FromMM(0.1))
        popt.SetAutoScale(False)
        popt.SetScale(1)
        popt.SetMirror(False)
        popt.SetUseGerberAttributes(True)
            
        popt.SetUseGerberProtelExtensions(False)
        popt.SetUseAuxOrigin(True)
        popt.SetSubtractMaskFromSilk(False)
        popt.SetDrillMarksType(pcbnew.DRILL_MARKS_NO_DRILL_SHAPE)

        self.report(15)
        for layer_info in plotPlan:
            if board.IsLayerEnabled(layer_info[1]):
                pctl.SetLayer(layer_info[1])
                pctl.OpenPlotfile(
                    layer_info[0],
                    pcbnew.PLOT_FORMAT_GERBER,
                    layer_info[2])
                pctl.PlotLayer()

        pctl.ClosePlot()

        # Write excellon drill files
        self.report(20)
        drlwriter = pcbnew.EXCELLON_WRITER(board)

        # mirrot, header, offset, mergeNPTH
        drlwriter.SetOptions(
            False,
            True,
            board.GetDesignSettings().GetAuxOrigin(),
            False)
        drlwriter.SetFormat(False)
        drlwriter.CreateDrillandMapFilesSet(pctl.GetPlotDirName(), True, False)

        # # Write netlist to enable Smart Tests
        self.report(25)
        netlist_writer = pcbnew.IPC356D_WRITER(board)
        netlist_writer.Write(os.path.join(temp_dir, netlistFilename))

        # # Export component list
        self.report(30)
        components = []
        if hasattr(board, 'GetModules'):
            footprints = list(board.GetModules())
        else:
            footprints = list(board.GetFootprints())

        for i, f in enumerate(footprints):
            try:
                footprint_name = str(f.GetFPID().GetFootprintName())
            except AttributeError:
                footprint_name = str(f.GetFPID().GetLibItemName())

            layer = {
                pcbnew.F_Cu: 'top',
                pcbnew.B_Cu: 'bottom',
            }.get(f.GetLayer())

            attrs = f.GetAttributes()
            parsed_attrs = self.parse_attrs(attrs)

            mount_type = 'smt' if parsed_attrs['smd'] else 'tht'  # Note: if not smd nor tht its 'other'. Consider other as tht.
            placed = not parsed_attrs['not_in_bom']

            components.append({
                'pos_x': (f.GetPosition()[0] - board.GetDesignSettings().GetAuxOrigin()[0]) / 1000000.0,
                'pos_y': (f.GetPosition()[1] - board.GetDesignSettings().GetAuxOrigin()[1]) * -1.0 / 1000000.0,
                'rotation': f.GetOrientation().AsDegrees(),
                'side': layer,
                'designator': f.GetReference(),
                'mpn': self.getMpnFromFootprint(f),
                'pack': footprint_name,
                'value': f.GetValue(),
                'mount_type': mount_type,
                'place': placed

            })

        with open((os.path.join(temp_dir, componentsFilename)), 'w') as outfile:
            json.dump(components, outfile)

        # # Create ZIP file
        zip_file = shutil.make_archive(temp_file, 'zip', temp_dir)
        props = board.GetProperties()
        if props.has_key('aisler_export_locally'):
            if props['aisler_export_locally'] is not '':
                path = os.path.dirname(os.path.abspath(board.GetFileName())) + '/' + props['aisler_export_locally']
                path = os.path.normpath(path)
                if not os.path.isdir(path):
                    os.makedirs(path)
            else:
                path = os.path.dirname(os.path.abspath(board.GetFileName()))                
            filename = "aisler_export_" + os.path.splitext(os.path.basename(board.GetFileName()))[0] + '.zip'
            shutil.copy(zip_file, os.path.join(path, filename))
            self.report(-1)
        else:
            self.push_to_webservice(zip_file, project_id, board)
            
        # delete temporary data 
        os.remove(zip_file)
        os.remove(temp_file)
        shutil.rmtree(temp_dir, ignore_errors = True)

    def push_to_webservice(self, zip_file, project_id, board):
        title_block = board.GetTitleBlock()
        files = {'upload[file]': open(zip_file, 'rb')}

        self.report(40)
        if project_id:
            data = {}
            data['upload_url'] = baseUrl + '/p/' + project_id + '/uploads.json'
        else:
            rsp = requests.get(baseUrl + '/p/new.json?ref=KiCadPush')
            data = json.loads(rsp.content)
            if not title_block.GetComment(commentLineIdx):
                title_block.SetComment(
                    commentLineIdx,
                    'AISLER Project ID: ' +
                    data['project_id'])

        title = title_block.GetTitle()
        if title == '':
            title = os.path.splitext(os.path.basename(board.GetFileName()))[0]

        rsp = requests.post(
            data['upload_url'], files=files, data={
                'upload[title]': title})
        urls = json.loads(rsp.content)
        progress = 0
        while progress < 100:
            time.sleep(pollingInterval)
            progress = json.loads(
                requests.get(
                    urls['callback']).content)['progress']
            self.report(int(40 + progress / 1.7))

        webbrowser.open(urls['redirect'])
        self.report(-1)

    def report(self, status):
        wx.PostEvent(self.wxObject, ResultEvent(status))
        
    def getMpnFromFootprint(self, f):
        keys = ['mpn', 'MPN', 'Mpn', 'AISLER_MPN']
        for key in keys:
            if f.HasFieldByName(key):
                return f.GetFieldByName(key).GetText()

    def parse_attr_flag(self, attr, mask):
        return mask == (attr & mask)

    def parse_attrs(self, attrs):
        return {} if not isinstance(attrs, int) else {
            'tht': self.parse_attr_flag(attrs, pcbnew.FP_THROUGH_HOLE),
            'smd': self.parse_attr_flag(attrs, pcbnew.FP_SMD),
            'not_in_pos': self.parse_attr_flag(attrs, pcbnew.FP_EXCLUDE_FROM_POS_FILES),
            'not_in_bom': self.parse_attr_flag(attrs, pcbnew.FP_EXCLUDE_FROM_BOM),
            'not_in_plan': self.parse_attr_flag(attrs, pcbnew.FP_BOARD_ONLY)
        }

