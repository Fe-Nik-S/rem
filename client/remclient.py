#coding: utf-8
"""Library for communicating with Robust Execute Manager

Краткое описание работы с библиотекой

Первые шаги.
Первым делом следует создать объект-коннектор, используя URL-сервера.
    conn = Connector("http://localhost:8104/")
В дальнейшем через коннектор можно получить доступ к конкретной очереди (conn.Queue(qname)), 
тэгу (conn.Tag(tagname)), создать пакет(conn.Packet(...)) или получить список зарегистрированных
на сервере очередей или тэгов(conn.ListObjects("queues") и conn.ListObjects("tags") соответственно).

Создание пакета.
    PACK_PRIOR = time.time()
    #создаётся пакет с именем packet-name, приориететом выполнения PACK_PRIOR, 
    #  начало выполнения пакета должно быть отложено до момента, когда будут установлены все тэги "tag1", "tag2" и "tag3"
    #  и в случае успешного выполнения пакета следует установить тэг "tag4"
    # kill_all_jobs_on_error - при неудачном завершении задания остальные задания прекращают работу.
    pack = conn.Packet("packet-name", PACK_PRIOR, wait_tags = ["tag1", "tag2", "tag3"], set_tag = "tag4")
    #добавление задач в пакет
    #параметры метода AddJob:
    #  shell - коммандная строка, которую следует выполнить
    #  tries - количество попыток выполнения команды (в случае неуспеха команда перазапускается ограниченное число раз) (по умолчанию: 5)
    #  parents - задания, которые должны быть выполнены до начала создаваемого
    #  pipe_parents - список заданий stdout, которых должен быть передан на вход исполняемой коммандной строке (строго в указанном порядке)
    #  set_tag - тэг, который будет установлен в случае успешного выполнения задания
    #  pipe_fail - аналог "set -o pipefail" для bash (работает только в случае, если bash установлен на сервере с REM'ом)
    #  description - опциональный параметр, задающий человекочитамое имя джоба
    #  files - список файлов, которые нужно положить в рабочую директорию задания (рабочая директория у всех заданий внутри одного пакета одна и та же)
    #          можно вместо списка указать dictionary, в этом случае значение словаря будет указывать на путь до файла, а ключ на имя, с которым этот файл следует положить 
    #          в рабочий каталог задания (реально в рабочем каталоге создаются symlink'и на файлы, располагающиеся в одной общей директории, куда копируются все бинарники)
    job0 = pack.AddJob(shell = "some_cmd")
    job1 = pack.AddJob(shell = "some_else_cmd", tries = 3)
    job2 = pack.AddJob(shell = "aggregate_programm", parents = [job0, job1], pipe_parents = [job1, job0], set_tag = "aggregate_done", files = ["local/path/to/aggregate_programm"])
    #файлы в рабочую директорию пакета можно добавить альтернативной функцией AddFiles
    pack.AddFiles(files = {"some_cmd": "local/path/to/some_cmd", "some_else_cmd": "local/path/to/some_else_cmd"})
    #Добавление задания в очередь выполнения задач (с этого момента пакет может начать исполняться, если, конечно, выполнено условие установки стартовых тэгов)
    conn.Queue("queue-name").AddPacket(pack)

Установка и просмотр тэгов.
    #проверка тэга (установлен или нет)
    conn.Tag("tag1").Check()
    #установка тэга
    conn.Tag("tag1").Set()
    #просмотр всех зарегистрированных тэгов и их значений
    print conn.ListObjects("tags")

Операции для процессинга работающих очередей
    #список всех очередей
    print conn.ListObjects("queues")
    #получить прокси-объект для работы с конкретной очередью
    queue = conn.Queue("queue-name")
    #распечатать краткий статус
    print queue.Status()
    #приостановить выполнение новых заданий в очереди (не влияет на уже запущенные)
    queue.Suspend()
    #возобновить выполнение новых заданий в очереди
    queue.Resume()
    #получить список всех пакетов задач в очереди
    for pack in queue.ListPackets("all"):
    # распечатать доступные данные о пакетах
        print pack.state, pack.wait, pack.name, pack.priority, pack.pck_id
        if pack.state != "CREATED":
            for job in pack.jobs:
                print job.state, job.shell, job.results
    # приостановить запущенные
        if pack.state in ("WORKABLE", "PENDING"):
            pack.Suspend()
    # возобновить работу приостановленных
        if pack.state == "SUSPENDED":
            pack.Resume()
    # удалить пакеты с ошибками
        if pack.state == "ERROR":
            pack.Delete()

Жизненный цикл пакета задач.
Возможные состояния пакета:
    CREATED     - пакет только создан, но еще не добавлен ни в одну из очередей (наполняется заданиями)
    WORKABLE    - рабочее состояние пакета (на данный момент в пакете нет задач для выполнения: ожидается выполнение уже запущенных задач)
    PENDING     - рабочее состояние пакета (есть задачи, ждущие своего выполнения)
    SUSPENDED   - выполнение новых задач приостановлено (вручную), либо ожидается установка необходимых стартовых тэгов
    ERROR       - возникла невосстановимая автоматически ошибка выполнения пакета задач (после разрешения задачи вручную невополненные задачи 
                    можно запустить заново через последовательность команд: pack.Suspend(); pack.Resume()
    SUCCESSFULL - пакет задач выполнен успешно
    HISTORIED   - пакет задач удален из очереди выполнения
"""
from __future__ import with_statement
import xmlrpclib
import time
import os
import re
import hashlib
import getpass
import types
import socket
import sys
import itertools
import warnings

__all__ = ["AdminConnector", "Connector"]
MAX_PRIORITY = 2**31 - 1

def create_connection_nodelay(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """source_address argument used only for python2.7 compatibility"""
    msg = "getaddrinfo returns an empty list"
    host, port = address
    for res in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        af, socktype, proto, canonname, sa = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            sock.connect(sa)
            return sock

        except socket.error, msg:
            if sock is not None:
                sock.close()
    raise socket.error, msg   

socket.create_connection = create_connection_nodelay




class Queue(object):
    """прокси объект для работы с очередями REM"""

    def __init__(self, connector, name):
        self.conn = connector
        self.proxy = connector.proxy
        self.name = name

    def AddPacket(self, pck):
        """добавляет в очередь созданный пакет, см. класс JobPacket"""
        self.proxy.pck_addto_queue(pck.id, self.name)

    def Suspend(self):
        """приостанавливает выполнение новых задач из очереди"""
        self.proxy.queue_suspend(self.name)

    def Resume(self):
        """возобновляет выполнение новых задач из очереди"""
        self.proxy.queue_resume(self.name)

    def Status(self):
        """возвращает краткую информацию о запущенных/выполненных задачах"""
        return self.proxy.queue_status(self.name)

    def ListPackets(self, filter, name_regex=None, prefix=None):
        """возвращает список пакетов из очереди, подпадающих под действие фильтра
        возможные значения парметра filter:
            all       - все пакеты
            errored   - пакеты, находящиеся в ошибочном статусе
            suspended - приостановленные пакеты
            worked    - отработавшие пакеты
            pending   - пакеты с готовыми для выполнения задачами
            waiting   - пакеты, ожидающие таймаут, после возникшей ошибки
            working   - пакеты, работающие в данный момент
        возвращается список объектов типа JobPacketInfo"""
        assert filter in ("errored", "suspended", "worked", "waiting", "pending", "working", "all")
        plist = self.proxy.queue_list(self.name, filter, name_regex, prefix)
        return [JobPacketInfo(self.conn, pck_id) for pck_id in plist]

    def ChangeWorkingLimit(self, lmtValue):
        """изменяет runtime лимит - одновременно запущенных задач из очереди"""
        self.proxy.queue_change_limit(self.name, int(lmtValue))

    def Delete(self):
        """удаляет на сервере очередь с данным именем (если таковая есть)
           в случае, если очередь не пуста, то удаление не произойдёт и кинется исключение"""
        self.proxy.queue_delete(self.name)

    def ListUpdated(self, last_modified, filter=None):
        if filter:
            assert filter in ("errored", "suspended", "worked", "waiting", "pending", "working", "all")
        plist = self.proxy.queue_list_updated(self.name, last_modified, filter)
        return [JobPacketInfo(self.conn, pck_id) for pck_id in plist]


class JobPacket(object):
    """прокси объект для создания пакетов задач REM"""
    DEFAULT_TRIES_COUNT = 5
    
    def __init__(self, connector, name, priority, notify_emails, wait_tags, set_tag, check_tag_uniqueness=False, kill_all_jobs_on_error=True):
        self.conn = connector
        self.proxy = connector.proxy
        if check_tag_uniqueness and self.proxy.check_tag(set_tag):
            raise RuntimeError("result tag %s already set for packet %s" % (set_tag, name))
        self.id = self.proxy.create_packet(name, priority, notify_emails, wait_tags, set_tag, kill_all_jobs_on_error)


    def AddJob(self, shell, parents = [], pipe_parents = [], set_tag = None, tries = DEFAULT_TRIES_COUNT, files = None, \
               max_err_len=None, retry_delay=None, pipe_fail=False, description="", notify_timeout=604800, max_working_time=1209600):
        """добавляет задачу в пакет
        shell - коммандная строка, которую следует выполнить
        tries - количество попыток выполнения команды (в случае неуспеха команда перазапускается ограниченное число раз) (по умолчанию: 5)
        parents - задания, которые должны быть выполнены до начала создаваемого
        pipe_parents - список заданий stdout, которых должен быть передан на вход исполняемой коммандной строке (строго в указанном порядке)
        set_tag - тэг, который будет установлен в случае успешного выполнения задания
        pipe_fail - аналог "set -o pipefail" для bash (работает только в случае, если bash установлен на сервере с REM'ом)
        description - опциональный параметр, задающий человекочитамое имя джоба
        files - список файлов, которые нужно положить в рабочую директорию задания (рабочая директория у всех заданий внутри одного пакета одна и та же)
               можно вместо списка указать dictionary, в этом случае значение словаря будет указывать на путь до файла, а ключ на имя, с которым этот файл следует положить 
               в рабочий каталог задания (реально в рабочем каталоге создаются symlink'и на файлы, располагающиеся в одной общей директории, куда копируются все бинарники)"""
        parents = [job.id for job in parents]
        pipe_parents = [job.id for job in pipe_parents]
        if files is not None:
            self.AddFiles(files)
        return JobInfo(id = self.proxy.pck_add_job(self.id, shell, parents,
                       pipe_parents, set_tag, tries, max_err_len, retry_delay,
                       pipe_fail, description, notify_timeout, max_working_time))

    def AddJobsBulk(self, *jobs):
        """быстрое(batch) добавление задач в пакет
        принимает неограниченное количество параметров, 
        каждый параметр - словарь, ключи и значения которого аналогичны параметрам метода AddJob"""
        multicall = xmlrpclib.MultiCall(self.proxy)
        for job in jobs:
            if "files" in job:
                self.AddFiles(job["files"])
            parents = [pj.id for pj in job.get("parents", [])]
            pipe_parents = [pj.id for pj in job.get("pipe_parents", [])]
            multicall.pck_add_job(self.id, job["shell"], parents, pipe_parents,
                                  job.get("set_tag", None),
                                  job.get("tries", self.DEFAULT_TRIES_COUNT),
                                  job.get("max_err_len", None),
                                  job.get("retry_delay", None))
        return multicall()

    def AddFiles(self, files, retries=1):
        """добавляет файлы, необходимые для выполнения пакета
        принимает один параметр files - полностью аналогичный одноименному параметру для AddJob"""
        JobPacketInfo(self.conn, self.id).AddFiles(files, retries)


class JobPacketInfo(object):
    """прокси объект для манипулирования пакетом задач в REM
    Объекты этого класса не нужно создавать вручную, правильный способ их получать - метод Queue.ListPackets"""
    DEF_INFO_TIMEOUT = 1800
    DEF_ATTRS = set(["pck_id", "proxy", "updStamp", "update","__dict__", "Suspend", "Resume", "Restart", "RestartFromErrors", "Delete", "AddFiles", "multiupdate", "__setstatus__"])

    def __init__(self, connector, pck_id):
        self.pck_id = pck_id
        self.conn = connector
        self.proxy = connector.proxy
        self.updStamp = 0

    def __getattribute__(self, attr):
        if attr not in JobPacketInfo.DEF_ATTRS \
           and time.time() - self.updStamp > JobPacketInfo.DEF_INFO_TIMEOUT:
            self.update()
        return object.__getattribute__(self, attr)

    def __setstatus__(self, status):
        for jobinfo in status.get("jobs", []):
            if "wait_jobs" in jobinfo:
                jobinfo["wait_jobs"] = [JobInfo(id = jobId) for jobId in jobinfo["wait_jobs"]]
        status["jobs"] = [JobInfo(**jobinfo) for jobinfo in status.get("jobs", [])]
        self.__dict__.update(status)
        self.updStamp = time.time()

    @classmethod
    def multiupdate(cls, objects, verbose=True):
        first = None
        for obj in objects:
            if first is None:
                first = obj
            if first.proxy is not obj.proxy:
                raise RuntimeError("multiupdate method can process only jobs from the same server")
        if first is None:
            return set()#nothing to do
        multicall = xmlrpclib.MultiCall(first.proxy)
        for obj in objects:
            multicall.pck_status(obj.pck_id)
        multicall_iterator = multicall()
        goodObjects = set()
        for index in xrange(len(objects)):
            try:
                pck_status = multicall_iterator[index]
                obj = objects[index]
                obj.__setstatus__(pck_status)
                goodObjects.add(obj)
            except xmlrpclib.Fault, e:
                if verbose:
                    print >>sys.stderr, "multicall exception raised: %s" % e 
        return goodObjects

    def update(self):
        """принудительный апдейт информации об объекте (xmlrpc-вызов)"""
        self.__setstatus__(self.proxy.pck_status(self.pck_id))

    def Suspend(self, kill_jobs=False):
        """приостанавливает выполнение пакета"""
        if kill_jobs:
            warnings.warn("packet.Suspend(kill_jobs=True) is deprecated, use packet.Stop()", DeprecationWarning)
        self.proxy.pck_suspend(self.pck_id, kill_jobs)
        self.update()

    def Stop(self):
        """приостанавливает выполнение пакета и убивает запущенные процессы"""
        self.proxy.pck_suspend(self.pck_id, True)
        self.update()

    def Resume(self):
        """возобновляет выполнение пакета"""
        self.proxy.pck_resume(self.pck_id)
        self.update()

    def Restart(self):
        """рестарт выполнения (перезапустит все job'ы, в том числе выполняющиеся и уже выполненные"""
        self.proxy.pck_reset(self.pck_id)
        self.proxy.pck_resume(self.pck_id)
        self.update()
        return True

    def RestartFromErrors(self, withException=True):
        """рестарт выполнения (только для пакетов в состоянии errored)"""
        if self.state in ["ERROR"]:
            self.proxy.pck_suspend(self.pck_id)
            self.proxy.pck_resume(self.pck_id)
            self.update()
            return True
        if withException:
            raise RuntimeError("can't restart packet with state %s" % self.state)
        return False

    def MoveToQueue(self, src_queue, dst_queue):
        return self.proxy.pck_moveto_queue(self.pck_id, src_queue, dst_queue)

    def Delete(self):
        """удаляет пакет (для работающих пакетов рекомендуется сначала выполнить Suspend())"""
        try:
            self.proxy.pck_delete(self.pck_id)
        except Exception, inst:
            raise RuntimeError(inst.faultString)

    @classmethod
    def _CalcFileChecksum(cls, path):
        BUF_SIZE = 256 * 1024
        with open(path, "r") as reader:
            cs_calc = hashlib.md5()
            while True:
                buff = reader.read(BUF_SIZE)
                if not buff:
                    break
                cs_calc.update(buff)
            return cs_calc.hexdigest()

    def _GetFileChecksum(self, path, db_path=None):
        if db_path is None:
            return self._CalcFileChecksum(path)
        
        try:
            import bsddb3
        except ImportError, e:
            if self.conn.verbose:
                print >>sys.stderr, "Can't import bsddb3 module: %r" % e
            return self._CalcFileChecksum(path)

        db = None
        try:
            db = bsddb3.btopen(db_path, 'c')

            last_modified = int(os.stat(path).st_mtime)
            val = db.get(path, None)
            if val is not None:
                (checksum, ts) = val.split('\t')
                if last_modified <= int(ts) <= time.time():
                    return checksum

            last_modified = int(os.stat(path).st_mtime)
            checksum = self._CalcFileChecksum(path)
            db[path] = '%s\t%d' % (checksum, last_modified)
            return checksum
        except bsddb3.db.DBError, e:
            if self.conn.verbose:
                print >>sys.stderr, "Failed obtaining checksum from bsddb3 db: %r" % e
            return self._CalcFileChecksum(path)
        finally:
            if db is not None:
                db.close()

    def _TryCheckBinaryAndLock(self, checksum, localPath):
        try:
            return self.proxy.check_binary_and_lock(checksum, localPath)
        except xmlrpclib.Fault, e:
            if self.conn.verbose:
                print >>sys.stderr, "check_binary_and_lock raised exception: code=%s descr=%s" % (e.faultCode, e.faultString)
            return False

    def _AddFiles(self, files):
        """добавляет или изменяет файлы, необходимые для работы пакета
        принимает один параметр files - полностью идентичный одноименному параметру для JobPacket.AddJob"""
        if not isinstance(files, dict):
            files = dict((os.path.split(file)[-1], file) for file in files)
        for fname, fpath in files.iteritems():
            if not os.path.isfile(fpath):
                raise AttributeError("can't find file \"%s\"" % fpath)

            checksum = self._GetFileChecksum(fpath, self.conn.checksumDbPath)
            if not self._TryCheckBinaryAndLock(checksum, fpath):
                data = open(fpath, 'r').read()
                checksum2 = hashlib.md5(data).hexdigest()
                if (checksum2 == checksum) or not self._TryCheckBinaryAndLock(checksum2, fpath):
                    self.proxy.save_binary(xmlrpclib.Binary(data))
                checksum = checksum2

            self.proxy.pck_add_binary(self.pck_id, fname, checksum)

    def AddFiles(self, files, retries=1):
        return _RetriableMethod(self._AddFiles, retries, True, AttributeError)(files)

    def ListFiles(self):
        return self.proxy.pck_list_files(self.pck_id)

    def GetFile(self, filename):
        binary = self.proxy.pck_get_file(self.pck_id, filename)
        data = binary.data
        return data

    def GetWorkingTime(self):
        def get_res_working_time(res):
            fmtTime = "%Y/%m/%d %H:%M:%S"
            reTimes = re.search("\"started:\s(.*);\sfinished:\s(.*);", res.data)
            if not reTimes:
                return 0
            return time.mktime(time.strptime(reTimes.group(2), fmtTime)) - time.mktime(time.strptime(reTimes.group(1), fmtTime))

        return sum(get_res_working_time(res) for res in itertools.chain(*(job.results for job in self.jobs)))


class JobInfo(object):
    """объект, инкапсулирующий информацию о задаче REM"""
    
    def __init__(self, **kws):
        self.__dict__.update(kws)


class Tag(object):
    """прокси объект для манипуляции тэгами"""

    def __init__(self, connector, name):
        self.conn = connector
        self.proxy = connector.proxy
        self.name = name

    def Check(self):
        """проверяет, установлен ли данный тэг"""
        return self.proxy.check_tag(self.name)

    def Set(self):
        """устанавливает тэг"""
        return self.proxy.set_tag(self.name)

    def Unset(self):
        """сбрасывает тэг"""
        return self.proxy.unset_tag(self.name)

    def Reset(self):
        """сброс тэга и остановка всех зависящих от него пакетов"""
        return self.proxy.reset_tag(self.name)

    def ListDependentPackets(self):
        """список id пакетов, которые будут запущены при установке данного пакета"""
        return self.get_dependent_packets_for_tag(self.name)


class TagsBulk(object):
    """Class for bulk operations on tags."""
    def __init__(self, conn, tags=None, name_regex=None, prefix=None):
        self.conn = conn
        if tags is not None:
            self.tags = list(tags)
        elif name_regex is not None or prefix is not None:
            self.tags = [tag for tag, state in conn.ListObjects("tags", name_regex, prefix)]
        else:
            self.tags = []

    def Check(self):
        multicall = xmlrpclib.MultiCall(self.conn.proxy)
        for tag in self.tags:
            multicall.check_tag(tag)
        multicall_iterator = multicall()
        self.states = dict(zip(self.tags, multicall_iterator))

    def FilterSet(self):
        self.Check()
        return TagsBulk(self.conn, filter(lambda x: self.states[x], self.tags))

    def FilterUnset(self):
        self.Check()
        return TagsBulk(self.conn, filter(lambda x: not self.states[x], self.tags))

    def Set(self):
        multicall = xmlrpclib.MultiCall(self.conn.proxy)
        for obj in self.tags:
            multicall.set_tag(obj)
        return multicall()

    def Unset(self):
        multicall = xmlrpclib.MultiCall(self.conn.proxy)
        for obj in self.tags:
            multicall.unset_tag(obj)
        return multicall()

    def Reset(self):
        multicall = xmlrpclib.MultiCall(self.conn.proxy)
        for obj in self.tags:
            multicall.reset_tag(obj)
        return multicall()

    def GetTags(self):
        return self.tags


class Connector(object):
    """объект коннектор, для работы с REM"""
    
    def __init__(self, url, conn_retries=5, verbose=False, checksumDbPath=None):
        """конструктор коннектора
        принимает один параметр - url REM сервера"""
        self.proxy = RetriableXMLRPCProxy(url, tries=conn_retries, verbose=verbose, allow_none=True)
        self.verbose = verbose
        self.checksumDbPath = checksumDbPath

    def Queue(self, qname):
        """возвращает объект для работы с очередью c именем qname (см. класс Queue)"""
        return Queue(self, qname)

    def Packet(self, pckname, priority = MAX_PRIORITY, notify_emails = [], wait_tags = (), set_tag = None, check_tag_uniqueness=False, kill_all_jobs_on_error=True):
        """создает новый пакет с именем pckname
            priority - приоритет выполнения пакета
            notify_emails - список почтовых адресов, для уведомления об ошибках
            wait_tags - список тэгов, установка которых является необходимым условием для начала выполнения пакета
            set_tag - тэг, устанавливаемый по завершении работы пакеты
            kill_all_jobs_on_error - при неудачном завершении задания остальные задания прекращают работу.
        возвращает объект класса JobPacket"""
        return JobPacket(self, pckname, priority, notify_emails, wait_tags, set_tag, check_tag_uniqueness, kill_all_jobs_on_error=kill_all_jobs_on_error)

    def Tag(self, tagname):
        """возвращает объект для работы с тэгом tagname (см. класс Tag)"""
        return Tag(self, tagname)

    def ListObjects(self, objtype, name_regex=None, prefix=None, memory_only=True):
        """возвращает список хранимых объектов верхнего уровня
            queues   - список очередей
            tags     - список тэгов
            schedule - список отложенных по времени заданий"""
        fn = getattr(self.proxy, "list_" + objtype, None)
        return fn(name_regex, prefix, memory_only)

    def PacketInfo(self, packet):
        """возвращает объект для манипуляций с пакетом (см. класс JobPacketInfo)
        принимает один параметр - объект типа JobPacket"""
        pck_id = packet.id if isinstance(packet, JobPacket) \
                        else packet if isinstance(packet, types.StringTypes) \
                        else None
        if pck_id is None:
            raise RuntimeError("can't create PacketInfo instance from %r" % packet)
        return JobPacketInfo(self, pck_id)

    def TagsBulk(self, *args, **kws):
        return TagsBulk(self, *args, **kws)


class ServerInfo(object):
    def __init__(self, **kws):
        self.__dict__.update(kws)


class AdminConnector(object):
    def __init__(self, url, conn_retries=5, verbose=False):
        self.proxy = RetriableXMLRPCProxy(url, tries=conn_retries, verbose=verbose, allow_none=True)

    def ListDeferedTags(self, name):
        """возвращает список тэгов, которые локально уже установились, но не все клиенты получили уведомление"""
        return self.proxy.list_shares(name)

    def ListSubscriptions(self, name):
        """возвращает список тэгов, на которые должны быть осуществлена подписка"""
        return self.proxy.list_subscriptions(name)

    def SuspendClient(self, name):
        """перестать определённому клиенту временно посылать уведомления о тэгах"""
        return self.proxy.suspend_client(name)

    def ResumeClient(self, name):
        """возобновить отправку уведомлений клиенту"""
        return self.proxy.resume_client(name)

    def ListClients(self):
        """возвращает топологию сети""" 
        return map(lambda x: ServerInfo(**x), self.proxy.list_clients())

    def ClientInfo(self, name):
        """возвращает информацию о клиенте"""
        return ServerInfo(**self.proxy.get_client_info(name))

    def ReloadConfig(self):
        """заставляет сервер пересчитать файл/svn на предмет появления новой информации о топологии сети"""
        return self.proxy.reload_config()

    def CheckConnection(self, clientname):
        """проверяет доступность сервера clientname"""
        return self.proxy.check_connection(clientname)


class _RetriableMethod:
    TIMEOUT = 30
    PROGR_MULT = 5
    @classmethod
    def __timeout__(cls, spentTrying):
        return cls.TIMEOUT + cls.PROGR_MULT ** spentTrying
    def __init__(self, method, tryCount, verbose, IgnoreExcType):
        self.method = method
        self.tryCount = tryCount
        self.verbose = verbose
        self.IgnoreExcType = IgnoreExcType
    def __getattr__(self, name):
        return _RetriableMethod(getattr(self.method, name), self.tryCount, self.verbose, self.IgnoreExcType)
    def __call__(self, *args):
        lastExc = None
        for trying in itertools.count():
            try: 
                return self.method(*args)
            except self.IgnoreExcType, lastExc:
                if self.verbose:
                    name = getattr(self.method, '_Method__name', None) or \
                            getattr(self.method, 'im_func', None)
                    print >>sys.stderr, "%s: execution for method %s failed [try: %d]\t%s" % (time.ctime(), name, trying, lastExc)
            if trying >= self.tryCount:
                break
            time.sleep(self.__timeout__(trying + 1))
        raise lastExc
        

class AuthTransport(xmlrpclib.Transport):
    def send_content(self, connection, request_body):
        connection.putheader("X-Username", getpass.getuser())
        connection.putheader("Content-Type", "text/xml")
        connection.putheader("Content-Length", str(len(request_body)))
        connection.endheaders()
        if request_body:
            connection.send(request_body)

class RetriableXMLRPCProxy(xmlrpclib.ServerProxy):
    
    def __init__(self, uri, tries, **kws):
        self.__maxTries = tries
        self.__verbose = kws.pop("verbose")
        kws["transport"] = AuthTransport()
        xmlrpclib.ServerProxy.__init__(self, uri, **kws)

    def __getattr__(self, name):
        return _RetriableMethod(xmlrpclib.ServerProxy.__getattr__(self, name), self.__maxTries, self.__verbose, socket.error)

