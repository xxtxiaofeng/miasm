import miasm2.jitter.jitcore as jitcore
import miasm2.expression.expression as m2_expr
import miasm2.jitter.csts as csts
from miasm2.expression.simplifications import expr_simp
from miasm2.ir.symbexec import symbexec


################################################################################
#                      Util methods for Python jitter                          #
################################################################################

def update_cpu_from_engine(cpu, exec_engine):
    """Updates @cpu instance according to new CPU values
    @cpu: JitCpu instance
    @exec_engine: symbexec instance"""

    for symbol in exec_engine.symbols:
        if isinstance(symbol, m2_expr.ExprId):
            if hasattr(cpu, symbol.name):
                value = exec_engine.symbols.symbols_id[symbol]
                if not isinstance(value, m2_expr.ExprInt):
                    raise ValueError("A simplification is missing: %s" % value)

                setattr(cpu, symbol.name, value.arg.arg)
        else:
            raise NotImplementedError("Type not handled: %s" % symbol)


def update_engine_from_cpu(cpu, exec_engine):
    """Updates CPU values according to @cpu instance
    @cpu: JitCpu instance
    @exec_engine: symbexec instance"""

    for symbol in exec_engine.symbols:
        if isinstance(symbol, m2_expr.ExprId):
            if hasattr(cpu, symbol.name):
                value = m2_expr.ExprInt_fromsize(symbol.size,
                                                 getattr(cpu, symbol.name))
                exec_engine.symbols.symbols_id[symbol] = value
        else:
            raise NotImplementedError("Type not handled: %s" % symbol)


################################################################################
#                              Python jitter Core                              #
################################################################################


class JitCore_Python(jitcore.JitCore):
    "JiT management, using Miasm2 Symbol Execution engine as backend"

    def __init__(self, my_ir, bs=None):
        super(JitCore_Python, self).__init__(my_ir, bs)
        self.symbexec = None

    def load(self, arch):
        "Preload symbols according to current architecture"

        symbols_init =  {}
        for i, r in enumerate(arch.regs.all_regs_ids_no_alias):
            symbols_init[r] = arch.regs.all_regs_ids_init[i]

        self.symbexec = symbexec(arch, symbols_init,
                                 func_read = self.func_read,
                                 func_write = self.func_write)

    def func_read(self, expr_mem):
        """Memory read wrapper for symbolic execution
        @expr_mem: ExprMem"""

        addr = expr_mem.arg.arg.arg
        size = expr_mem.size / 8
        value = self.vmmngr.vm_get_mem(addr, size)

        return m2_expr.ExprInt_fromsize(expr_mem.size,
                                        int(value[::-1].encode("hex"), 16))

    def func_write(self, symb_exec, dest, data, mem_cache):
        """Memory read wrapper for symbolic execution
        @symb_exec: symbexec instance
        @dest: ExprMem instance
        @data: Expr instance
        @mem_cache: dict"""

        # Get the content to write
        data = expr_simp(data)
        if not isinstance(data, m2_expr.ExprInt):
            raise NotImplementedError("A simplification is missing: %s" % data)
        to_write = data.arg.arg

        # Format information
        addr = dest.arg.arg.arg
        size = data.size / 8
        content = hex(to_write).replace("0x", "").replace("L", "")
        content = "0" * (size * 2 - len(content)) + content
        content = content.decode("hex")[::-1]

        # Write in VmMngr context
        self.vmmngr.vm_set_mem(addr, content)

    def jitirblocs(self, label, irblocs):
        """Create a python function corresponding to an irblocs' group.
        @label: the label of the irblocs
        @irblocs: a gorup of irblocs
        """

        def myfunc(cpu, vmmngr):
            """Execute the function according to cpu and vmmngr states
            @cpu: JitCpu instance
            @vm: VmMngr instance
            """

            # Keep current location in irblocs
            cur_label = label
            loop = True

            # Required to detect new instructions
            offsets_jitted = set()

            # Get exec engine
            exec_engine = self.symbexec

            # For each irbloc inside irblocs
            while loop is True:

                # Get the current bloc
                loop = False
                for irb in irblocs:
                    if irb.label == cur_label:
                        loop = True
                        break

                # Irblocs must end with returning an ExprInt instance
                assert(loop is not False)

                # Refresh CPU values according to @cpu instance
                update_engine_from_cpu(cpu, exec_engine)

                # Execute current ir bloc
                for ir, line in zip(irb.irs, irb.lines):

                    # For each new instruction (in assembly)
                    if line.offset not in offsets_jitted:
                        offsets_jitted.add(line.offset)

                        # Check for memory exception
                        if (vmmngr.vm_get_exception() != 0):
                            update_cpu_from_engine(cpu, exec_engine)
                            return line.offset

                    # Eval current instruction (in IR)
                    exec_engine.eval_ir(ir)

                    # Check for memory exception which do not update PC
                    if (vmmngr.vm_get_exception() & csts.EXCEPT_DO_NOT_UPDATE_PC != 0):
                        update_cpu_from_engine(cpu, exec_engine)
                        return line.offset

                # Get next bloc address
                ad = expr_simp(exec_engine.eval_expr(irb.dst))

                # Updates @cpu instance according to new CPU values
                update_cpu_from_engine(cpu, exec_engine)

                # Manage resulting address
                if isinstance(ad, m2_expr.ExprInt):
                    return ad.arg.arg
                elif isinstance(ad, m2_expr.ExprId):
                    cur_label = ad.name
                else:
                    raise NotImplementedError("Type not handled: %s" % ad)

        # Associate myfunc with current label
        self.lbl2jitbloc[label.offset] = myfunc

    def jit_call(self, label, cpu, vmmngr):
        """Call the function label with cpu and vmmngr states
        @label: function's label
        @cpu: JitCpu instance
        @vm: VmMngr instance
        """

        # Get Python function corresponding to @label
        fc_ptr = self.lbl2jitbloc[label]

        # Update memory state
        self.vmmngr = vmmngr

        # Execute the function
        return fc_ptr(cpu, vmmngr)