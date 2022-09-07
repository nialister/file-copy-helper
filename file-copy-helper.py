import os
import sys
import enum
import glob
import stat
import shutil
import fnmatch
import filecmp
from itertools import filterfalse
import argparse
import time


class Response(enum.Enum):
    Ok = 0
    SourceNotExist = 1
    UnknownType = 2
    UnknownMethod = 3
    Skip = 4


class Method:
    Copy = "copy"
    Move = "move"
    Link = "link"
    Symlink = "symlink"


class Statistics:
    def __init__(self):
        self.correct_lines = 0
        self.skipped_lines = 0
        self.incorrect_lines = 0
        self.total_lines = 0
        self.succeeded_transfers = 0
        self.skipped_transfers = 0
        self.failed_transfers = 0


class ArgumentParserError(Exception): pass


class ThrowingArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentParserError(message)


# compare two files
def compareFiles(file1, file2, shallow=True):
    return filecmp.cmp(file1, file2, shallow=shallow)


# compare two directories (reworked dircmp)
def compareDirs(dir1, dir2, shallow=True):
    dir1_list = os.listdir(dir1)
    dir2_list = os.listdir(dir2)
    dir1_list.sort()
    dir2_list.sort()

    a = dict(zip(map(os.path.normcase, dir1_list), dir1_list))
    b = dict(zip(map(os.path.normcase, dir2_list), dir2_list))
    common = list(map(a.__getitem__, filter(b.__contains__, a)))
    dir1_only = list(map(a.__getitem__, filterfalse(b.__contains__, a)))
    dir2_only = list(map(b.__getitem__, filterfalse(a.__contains__, b)))
    # if we have objects in only one directory then they are different
    if dir1_only or dir2_only:
        return False

    common_dirs = []
    common_files = []
    common_funny = []
    for x in common:
        a_path = os.path.join(dir1, x)
        b_path = os.path.join(dir2, x)

        ok = 1
        try:
            a_stat = os.stat(a_path)
        except OSError:
            ok = 0
        try:
            b_stat = os.stat(b_path)
        except OSError:
            ok = 0

        if ok:
            a_type = stat.S_IFMT(a_stat.st_mode)
            b_type = stat.S_IFMT(b_stat.st_mode)
            if a_type != b_type:
                common_funny.append(x)
            elif stat.S_ISDIR(a_type):
                common_dirs.append(x)
            elif stat.S_ISREG(a_type):
                common_files.append(x)
            else:
                common_funny.append(x)
        else:
            common_funny.append(x)
    # if we have invalid objects then report directories are different
    if common_funny:
        return False

    same_files, diff_files, funny_files = filecmp.cmpfiles(dir1, dir2, common_files, shallow=shallow)
    # if we have different files or invalid objects then report directories are different
    if diff_files or funny_files:
        return False

    # compare subdirs
    for x in common_dirs:
        a_x = os.path.join(dir1, x)
        b_x = os.path.join(dir2, x)
        # report if subdirs have differencies
        if not compareDirs(a_x, b_x, shallow=shallow):
            return False

    return True


# return list with filenames in path directory that match patterns
def ignoredNames(path: str, patterns):
    names = os.listdir(path)
    ignored_names = []
    for pattern in patterns:
        ignored_names.extend(fnmatch.filter(names, pattern))
    return set(ignored_names)


# transfer a file from src to dst
def transferFile(src, dst, method=Method.Copy, force=False):
    # check if dst object exists
    if os.path.exists(dst):
        if not force:
            # skip file if src and dst are equal
            if compareFiles(src, dst):
                return Response.Skip
        os.remove(dst)
    # if not, make sure we have dst dir
    else:
        dst_dirname, dst_basename = os.path.split(dst)
        if not os.path.exists(dst_dirname):
            os.makedirs(dst_dirname)
    # transfer file by selected method
    if method == Method.Link:
        os.link(src, dst)
    elif method == Method.Symlink:
        os.symlink(src, dst)
    elif method == Method.Copy:
        shutil.copy2(src, dst)
    elif method == Method.Move:
        shutil.move(src, dst)
    else:
        return Response.UnknownMethod
    return Response.Ok


# transfer a directory from src to dst
def transferDir(src, dst, method=Method.Copy, force=False, ignorepatterns=[], onlyfiles=False, deletedst=False,
                keeppatterns=[]):
    # check if dst object exists
    if os.path.exists(dst):
        # if they are the same then skip them if force is false
        if not force:
            if compareDirs(src, dst):
                return Response.Skip
        # delete dst dir content
        if deletedst:
            keep_names = ignoredNames(dst, keeppatterns)
            filenames = [f for f in os.listdir(dst) if f not in keep_names]
            for filename in filenames:
                filepath = os.path.join(dst, filename)
                if os.path.isfile(filepath) or os.path.islink(filepath):
                    os.remove(filepath)
                elif os.path.isdir(filepath):
                    shutil.rmtree(filepath)
    # transfer only files from src directory
    if onlyfiles:
        ignored_names = ignoredNames(src, ignorepatterns)
        filenames = [f for f in os.listdir(src)
                     if os.path.isfile(os.path.join(src, f)) and f not in ignored_names]
        for filename in filenames:
            resp = transferFile(os.path.join(src, filename), os.path.join(dst, filename), method=method)
            if resp is not Response.Ok:
                return resp
    # transfer whole src directory
    else:
        # transfer dir by selected method
        if method == Method.Link:
            shutil.copytree(src, dst, copy_function=os.link, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*ignorepatterns))
        elif method == Method.Symlink:
            shutil.copytree(src, dst, copy_function=os.symlink, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*ignorepatterns))
        elif method == Method.Copy:
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*ignorepatterns))
        elif method == Method.Move:
            shutil.copytree(src, dst, copy_function=shutil.move, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*ignorepatterns))
            shutil.rmtree(src)
        else:
            return Response.UnknownMethod
    return Response.Ok


# make transfer for file or directory
def makeTransfer(src, dst, method=Method.Copy, force=False, ignorepatterns=[], onlyfiles=False, deletedst=False,
                 keeppatterns=[]):
    # check source object existence
    if os.path.exists(src):
        # source objects is a file or a link
        if os.path.isfile(src) or os.path.islink(src):
            return transferFile(src, dst, method=method, force=force)
        # source object is a directory
        elif os.path.isdir(src):
            return transferDir(src, dst, method=method, force=force, ignorepatterns=ignorepatterns, onlyfiles=onlyfiles,
                               deletedst=deletedst, keeppatterns=keeppatterns)
        # unknown type of source object
        else:
            return Response.UnknownType
    # source object do not exist
    else:
        return Response.SourceNotExist


# parse line
def parseLine(line: str, lpars: ThrowingArgumentParser, lstat: Statistics):
    # check line len is correct
    if len(line) == 0:
        return
    # check comment
    if line[0] == '#':
        print("  Skip line: " + line[1:] + "")
        lstat.skipped_lines += 1
        return
    try:
        line_args = lpars.parse_args(line.split())
        input_path = line_args.input.strip().strip('"')
        output_path = line_args.output.strip().strip('"')
        if input_path == "" or output_path == "":
            raise Exception("Input or output is empty")

        method = line_args.method
        force = line_args.force
        ignorepatterns = [ip.strip().strip('"') for ip in line_args.ignorepatterns]
        onlyfiles = line_args.onlyfiles
        deletedst = line_args.deletedst
        keeppatterns = [kp.strip().strip('"') for kp in line_args.keeppatterns]

        print("  Handle line: " + line[1:] + "")
        print("    " + method.capitalize() + " \"" + input_path + "\" --> \"" + output_path + "\" ...")
        lstat.correct_lines += 1
        res = makeTransfer(input_path, output_path, method=method, force=force,
                           ignorepatterns=ignorepatterns, onlyfiles=onlyfiles, deletedst=deletedst,
                           keeppatterns=keeppatterns)
        if res == Response.Ok:
            lstat.succeeded_transfers += 1
            print("    Ok")
        elif res == Response.SourceNotExist:
            print("    Fail: source object not exist")
            lstat.failed_transfers += 1
        elif res == Response.UnknownType:
            print("    Fail: unknown type of source object ")
            lstat.failed_transfers += 1
        elif res == Response.UnknownMethod:
            print("    Fail: unknown transfer method")
            lstat.failed_transfers += 1
        elif res == Response.Skip:
            lstat.skipped_transfers += 1
            print("    Skip")
    except Exception as e:
        print("  Cannot handle line: " + line + ", because " + str(e))
        lstat.incorrect_lines += 1


if __name__ == '__main__':
    try:
        print("File-copy-helper script starts")

        parser = ThrowingArgumentParser(description='Arg parser')
        parser.add_argument('-l', '--lines', metavar='lines', nargs="+", default=[],
                            help='lines to parse')
        parser.add_argument('-f', '--files', metavar='files', nargs="+", default=[],
                            help='files with lines to parse')
        parser.add_argument('-d', '--dir', metavar='dir', type=str, default="",
                            help='directory with files, default: directory with script')
        parser.add_argument('-fp', '--filepattern', metavar='filepattern', type=str, default='*.txt',
                            help='pattern of files to parse lines, default: \'*.txt\'')
        parser.add_argument('-es', '--endsleep', metavar='endsleep', type=int, default='0',
                            help='sleep seconds at the end of script, default: 0')
        args = parser.parse_args()

        app_lines = args.lines
        print("App lines: " + str(app_lines))
        app_files = args.files
        print("App files: " + str(app_files))
        app_dirname, app_basename = os.path.split(sys.argv[0])
        app_dir = os.path.abspath(os.path.join(app_dirname, args.dir))
        print("App directory: " + app_dir)
        app_filepattern = args.filepattern
        print("App file pattern: " + app_filepattern)
        app_endsleep = int(args.endsleep)

        line_parser = ThrowingArgumentParser(description="Line parser")
        line_parser.add_argument('-i', '--input', metavar='input', type=str, default="",
                                 help="input path to file/directory")
        line_parser.add_argument('-o', '--output', metavar='output', type=str, default="",
                                 help="output path to file/directory")
        line_parser.add_argument('-m', '--method', metavar='method', type=str, default=Method.Copy,
                                 help="method for file transfer, available methods: \'" +
                                      Method.Link + "\' to make hardlink, \'" +
                                      Method.Symlink + "\' to make symbolic link, \'" +
                                      Method.Copy + "\' to copy, \'" + Method.Move + "\' to cut, "
                                      "default: \'" + Method.Copy + "\'")
        line_parser.add_argument('-f', '--force', action='store_true',
                                 help="force call transfer function if objects are the same")
        line_parser.add_argument('-ip', '--ignorepatterns', metavar='ignorepatterns', nargs="+", default=[],
                                 help="ignore patterns for skipping objects")
        line_parser.add_argument('-of', '--onlyfiles', action='store_true',
                                 help="transfer only files in directory")
        line_parser.add_argument('-dd', '--deletedst', action='store_true',
                                 help="delete destination content")
        line_parser.add_argument('-kp', '--keeppatterns', metavar='keeppatterns', nargs="+", default=[],
                                 help="keep patterns for objects in destination directory if -dd is active")

        stat = Statistics()
        # parse lines
        if len(app_lines):
            linelist = list(filter(None, (line.strip() for line in app_lines)))
            stat.total_lines += len(linelist)
            print("Parse " + str(len(linelist)) + " line(s) ...")
            for line in linelist:
                parseLine(line, line_parser, stat)
        # handle files
        else:
            linelist_filenames = []
            # handle separate files
            if len(app_files):
                print("Scan separate files ...")
                for af in app_files:
                    filename = af.strip().strip('"')
                    if not os.path.isabs(filename):
                        filename = os.path.join(app_dir, filename)
                    if os.path.exists(filename):
                        linelist_filenames.append(filename)
            # scan directory for files with selected filepattern
            else:
                print("Scan app directory ...")
                if not os.path.exists(app_dir):
                    raise Exception("Incorrect directory: " + app_dir)
                linelist_filenames = glob.glob(os.path.join(app_dir, app_filepattern))
            if len(linelist_filenames) == 0:
                print("No files to parse found")
            else:
                print("Found " + str(len(linelist_filenames)) + " file(s) to parse")
                # iterate over all line files
                for linelist_filename in linelist_filenames:
                    # open each line file
                    with open(linelist_filename, "r") as file:
                        linelist = list(filter(None, (line.strip() for line in file)))
                        stat.total_lines += len(linelist)
                        print("Handle file: \"" + linelist_filename + "\", lines to parse: " + str(len(linelist)))
                        # iterate over every line in file
                        for line in linelist:
                            parseLine(line, line_parser, stat)

        print("Correct/skipped/incorrect/total lines: " + str(stat.correct_lines) + "/" +
              str(stat.skipped_lines) + "/" + str(stat.incorrect_lines) + "/" + str(stat.total_lines) + ", \n"
              "Succeeded/skipped/incorrect/total transfers: " + str(stat.succeeded_transfers) + "/" +
              str(stat.skipped_transfers) + "/" + str(stat.incorrect_lines) + "/" + str(stat.correct_lines))
        time.sleep(app_endsleep)
    except Exception as e:
        print(str(e))
